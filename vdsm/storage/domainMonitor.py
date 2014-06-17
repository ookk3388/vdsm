#
# Copyright 2011 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import logging
import threading
import time
import weakref

from vdsm import utils
from vdsm.config import config

from . import misc
from .sdc import sdCache


class DomainMonitorStatus(object):
    __slots__ = (
        "error", "checkTime", "valid", "readDelay", "masterMounted",
        "masterValid", "diskUtilization", "vgMdUtilization",
        "vgMdHasEnoughFreeSpace", "vgMdFreeBelowThreashold", "hasHostId",
        "isoPrefix", "version",
    )

    def __init__(self):
        self.clear()

    def clear(self):
        self.error = None
        self.checkTime = time.time()
        self.valid = True
        self.readDelay = 0
        self.diskUtilization = (None, None)
        self.masterMounted = False
        self.masterValid = False
        self.hasHostId = False
        # FIXME : Exposing these breaks abstraction and is not
        #         needed. Keep exposing for BC. Remove and use
        #         warning mechanism.
        self.vgMdUtilization = (0, 0)
        self.vgMdHasEnoughFreeSpace = True
        self.vgMdFreeBelowThreashold = True
        # The iso prefix is computed asynchronously because in any
        # synchronous operation (e.g.: connectStoragePool, getInfo)
        # we cannot risk to stop and wait for the iso domain to
        # report its prefix (it might be unreachable).
        self.isoPrefix = None
        self.version = -1

    def update(self, st):
        for attr in self.__slots__:
            setattr(self, attr, getattr(st, attr))

    def copy(self):
        res = DomainMonitorStatus()
        res.update(self)
        return res


class DomainMonitor(object):
    log = logging.getLogger('Storage.DomainMonitor')

    def __init__(self, interval):
        self._domains = {}
        self._interval = interval
        self.onDomainStateChange = misc.Event(
            "Storage.DomainMonitor.onDomainStateChange")

    @property
    def monitoredDomains(self):
        return self._domains.keys()

    @property
    def poolMonitoredDomains(self):
        return [sdUUID for sdUUID, monitor in self._domains.items()
                if monitor.poolDomain]

    def startMonitoring(self, sdUUID, hostId, poolDomain=True):
        domainThread = self._domains.get(sdUUID)

        if domainThread is not None:
            domainThread.poolDomain |= poolDomain
            return

        self.log.info("Start monitoring %s", sdUUID)
        domainThread = DomainMonitorThread(weakref.proxy(self),
                                           sdUUID, hostId, self._interval)
        domainThread.poolDomain = poolDomain
        domainThread.start()
        # The domain should be added only after it succesfully started
        self._domains[sdUUID] = domainThread

    def stopMonitoring(self, sdUUIDs):
        sdUUIDs = frozenset(sdUUIDs)
        monitors = [monitor for monitor in self._domains.values()
                    if monitor.sdUUID in sdUUIDs]
        self._stopMonitors(monitors)

    def getMonitoredDomainsStatus(self):
        for sdUUID, monitor in self._domains.items():
            yield sdUUID, monitor.getStatus()

    def close(self):
        self.log.info("Stopping all domain monitors")
        self._stopMonitors(self._domains.values())

    def _stopMonitors(self, monitors):
        # The domain monitor issues events that might become raceful if
        # you don't wait until a monitor thread exit.
        # Eg: when a domain is detached the domain monitor is stopped and
        # the host id is released. If the monitor didn't actually exit it
        # might respawn a new acquire host id.

        # First stop monitor threads - this take no time, and make the process
        # about 7 times faster when stopping 30 monitors.
        for monitor in monitors:
            self.log.info("Stop monitoring %s", monitor.sdUUID)
            monitor.stop()

        # Now wait for threads to finish - this takes about 10 seconds with 30
        # monitors, most of the time spent waiting for sanlock.
        for monitor in monitors:
            self.log.debug("Waiting for monitor %s", monitor.sdUUID)
            monitor.join()
            try:
                del self._domains[monitor.sdUUID]
            except KeyError:
                self.log.warning("Montior for %s removed while stopping",
                                 monitor.sdUUID)


class DomainMonitorThread(object):
    log = logging.getLogger('Storage.DomainMonitorThread')

    def __init__(self, domainMonitor, sdUUID, hostId, interval):
        self.thread = threading.Thread(target=self._run)
        self.thread.setDaemon(True)
        self.domainMonitor = domainMonitor
        self.stopEvent = threading.Event()
        self.domain = None
        self.sdUUID = sdUUID
        self.hostId = hostId
        self.interval = interval
        self.firstChange = True
        self.status = DomainMonitorStatus()
        self.nextStatus = DomainMonitorStatus()
        self.isIsoDomain = None
        self.isoPrefix = None
        self.lastRefresh = time.time()
        self.refreshTime = \
            config.getint("irs", "repo_stats_cache_refresh_timeout")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopEvent.set()

    def join(self):
        self.thread.join()

    def getStatus(self):
        return self.status.copy()

    def __canceled__(self):
        """ Accessed by methods decorated with @util.cancelpoint """
        return self.stopEvent.is_set()

    @utils.traceback(on=log.name)
    def _run(self):
        self.log.debug("Starting domain monitor for %s", self.sdUUID)
        try:
            self._monitorLoop()
        finally:
            self.log.debug("Stopping domain monitor for %s", self.sdUUID)
            if self._shouldReleaseHostId():
                self._releaseHostId()

    def _monitorLoop(self):
        while not self.stopEvent.is_set():
            try:
                self._monitorDomain()
            except utils.Canceled:
                self.log.debug("Canceled domain monitor for %s", self.sdUUID)
                return
            except:
                self.log.error("The domain monitor for %s failed unexpectedly",
                               self.sdUUID, exc_info=True)
            self.stopEvent.wait(self.interval)

    def _monitorDomain(self):
        self.nextStatus.clear()

        # Pick up changes in the domain, for example, domain upgrade.
        if self._shouldRefreshDomain():
            self._refreshDomain()

        try:
            # We should produce the domain inside the monitoring loop because
            # it might take some time and we don't want to slow down the thread
            # start (and anything else that relies on that as for example
            # updateMonitoringThreads). It also might fail and we want keep
            # trying until we succeed or the domain is deactivated.
            if self.domain is None:
                self._produceDomain()

            # The isIsoDomain assignment is delayed because the isoPrefix
            # discovery might fail (if the domain suddenly disappears) and we
            # could risk to never try to set it again.
            if self.isIsoDomain is None:
                self._setIsoDomainInfo()

            self._performDomainSelftest()
            self._checkReadDelay()
            self._collectStatistics()
        except Exception as e:
            self.log.error("Error while collecting domain %s monitoring "
                           "information", self.sdUUID, exc_info=True)
            self.nextStatus.error = e

        self.nextStatus.checkTime = time.time()
        self.nextStatus.valid = (self.nextStatus.error is None)

        if self._statusDidChange():
            self._notifyStatusChanges()
        self.firstChange = False

        if self._shouldAcquireHostId():
            self._acquireHostId()

        self.status.update(self.nextStatus)

    # Notifiying status changes

    def _statusDidChange(self):
        return self.firstChange or self.status.valid != self.nextStatus.valid

    @utils.cancelpoint
    def _notifyStatusChanges(self):
        self.log.debug("Domain %s changed its status to %s", self.sdUUID,
                       "Valid" if self.nextStatus.valid else "Invalid")
        try:
            self.domainMonitor.onDomainStateChange.emit(
                self.sdUUID, self.nextStatus.valid)
        except:
            self.log.warn("Could not emit domain state change event",
                          exc_info=True)

    # Refreshing domain

    def _shouldRefreshDomain(self):
        return time.time() - self.lastRefresh > self.refreshTime

    @utils.cancelpoint
    def _refreshDomain(self):
        self.log.debug("Refreshing domain %s", self.sdUUID)
        sdCache.manuallyRemoveDomain(self.sdUUID)
        self.lastRefresh = time.time()

    # Deferred initialization

    @utils.cancelpoint
    def _produceDomain(self):
        self.domain = sdCache.produce(self.sdUUID)

    @utils.cancelpoint
    def _setIsoDomainInfo(self):
        isIsoDomain = self.domain.isISO()
        if isIsoDomain:
            self.isoPrefix = self.domain.getIsoDomainImagesDir()
        self.isIsoDomain = isIsoDomain

    # Collecting monitoring info

    @utils.cancelpoint
    def _performDomainSelftest(self):
        # This may trigger a refresh of lvm cache. We have seen this taking up
        # to 90 seconds on overloaded machines.
        self.domain.selftest()

    @utils.cancelpoint
    def _checkReadDelay(self):
        # This may block for long time if the storage server is not accessible.
        # On overloaded machines we have seen this take up to 15 seconds.
        self.nextStatus.readDelay = self.domain.getReadDelay()

    def _collectStatistics(self):
        stats = self.domain.getStats()
        self.nextStatus.diskUtilization = (stats["disktotal"],
                                           stats["diskfree"])

        self.nextStatus.vgMdUtilization = (stats["mdasize"],
                                           stats["mdafree"])

        self.nextStatus.vgMdHasEnoughFreeSpace = stats["mdavalid"]
        self.nextStatus.vgMdFreeBelowThreashold = stats["mdathreshold"]

        masterStats = self.domain.validateMaster()
        self.nextStatus.masterValid = masterStats['valid']
        self.nextStatus.masterMounted = masterStats['mount']

        self.nextStatus.hasHostId = self.domain.hasHostId(self.hostId)
        self.nextStatus.isoPrefix = self.isoPrefix
        self.nextStatus.version = self.domain.getVersion()

    # Managing host id

    def _shouldAcquireHostId(self):
        # An ISO domain can be shared by multiple pools
        return (not self.isIsoDomain and
                self.nextStatus.valid and
                self.nextStatus.hasHostId is False)

    def _shouldReleaseHostId(self):
        # If this is an ISO domain we didn't acquire the host id and releasing
        # it is superfluous.
        return self.domain and not self.isIsoDomain

    @utils.cancelpoint
    def _acquireHostId(self):
        try:
            self.domain.acquireHostId(self.hostId, async=True)
        except:
            self.log.debug("Unable to issue the acquire host id %s "
                           "request for domain %s", self.hostId,
                           self.sdUUID, exc_info=True)

    def _releaseHostId(self):
        try:
            self.domain.releaseHostId(self.hostId, unused=True)
        except:
            self.log.debug("Unable to release the host id %s for domain "
                           "%s", self.hostId, self.sdUUID, exc_info=True)
