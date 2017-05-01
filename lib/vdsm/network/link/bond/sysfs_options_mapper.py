# Copyright 2017 Red Hat, Inc.
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
from __future__ import absolute_import

from contextlib import contextmanager
import errno
from functools import partial
from glob import iglob
import io
import json
import os

import six

from vdsm import constants
from vdsm.common.cache import memoized

from vdsm.network.link.bond import sysfs_options
from vdsm.network.link.bond.sysfs_driver import BONDING_MASTERS
from vdsm.network.link.iface import random_iface_name

BONDING_NAME2NUMERIC_PATH = constants.P_VDSM + 'bonding-name2numeric.json'

_MAX_BONDING_MODES = 6


def dump_bonding_options():
    jdump = partial(json.dump,
                    sort_keys=True, indent=4, separators=(',', ': '))
    with open(sysfs_options.BONDING_DEFAULTS, 'w') as f:
        jdump(_get_default_bonding_options(), f)

    with open(BONDING_NAME2NUMERIC_PATH, 'w') as f:
        jdump(_get_bonding_options_name2numeric(), f)


def _get_default_bonding_options():
    """
    Return default options per mode, in a dictionary of dictionaries. All keys
    are strings.
    """
    bond_name = random_iface_name()
    with _bond_device(bond_name):
        default_mode = sysfs_options.properties(bond_name, ('mode',))['mode']

    # read default values for all modes
    opts = {}
    for mode in range(_MAX_BONDING_MODES + 1):
        mode = str(mode)
        # The bond is created per mode to resolve an EBUSY error
        # that appears randomly when changing bond mode and modifying its
        # attributes. (Seen only on CI runs)
        with _bond_device(bond_name, mode):
            opts[mode] = sysfs_options.properties(
                bond_name,
                filter_out_properties=sysfs_options.EXCLUDED_BONDING_ENTRIES)
            opts[mode]['mode'] = default_mode

    return opts


def _get_bonding_options_name2numeric():
    """
    Return a map of options values per mode, in a dictionary of dictionaries.
    All keys are strings.
    """
    bond_name = random_iface_name()
    opts = {}
    for mode in range(_MAX_BONDING_MODES + 1):
        mode = str(mode)
        # The bond is created per mode to resolve an EBUSY error
        # that appears randomly when changing bond mode and modifying its
        # attributes. (Seen only on CI runs)
        with _bond_device(bond_name, mode):
            opts[mode] = _bond_opts_name2numeric_filtered(bond_name)

    return opts


@contextmanager
def _bond_device(bond_name, mode=None):
    with open(BONDING_MASTERS, 'w') as bonds:
        bonds.write('+' + bond_name)

    if mode is not None:
        _change_mode(bond_name, mode)
    try:
        yield
    finally:
        with open(BONDING_MASTERS, 'w') as bonds:
            bonds.write('-' + bond_name)


def _change_mode(bond_name, mode):
    with open(sysfs_options.BONDING_OPT % (bond_name, 'mode'), 'w') as opt:
        opt.write(mode)


def _bond_opts_name2numeric_filtered(bond):
    """
    Return a dictionary in the same format as _bond_opts_name2numeric().
    Exclude entries that are not bonding options,
    e.g. 'ad_num_ports' or 'slaves'.
    """
    return dict(((opt, val) for (opt, val)
                 in six.iteritems(_bond_opts_name2numeric(bond))
                 if opt not in sysfs_options.EXCLUDED_BONDING_ENTRIES))


def get_bonding_option_numeric_val(mode_num, option_name, val_name):
    bond_opts_map = _get_bonding_option_name2numeric()
    opt = bond_opts_map[mode_num].get(option_name, None)
    return opt.get(val_name, None) if opt else None


@memoized
def _get_bonding_option_name2numeric():
    """
    Return options per mode, in a dictionary of dictionaries.
    For each mode, there are options with name values as keys
    and their numeric equivalent.
    """
    with open(BONDING_NAME2NUMERIC_PATH) as f:
        return json.loads(f.read())


def _bond_opts_name2numeric(bond):
    """
    Returns a dictionary of bond option name and a values iterable. E.g.,
    {'mode': ('balance-rr', '0'), 'xmit_hash_policy': ('layer2', '0')}
    """
    bond_mode_path = sysfs_options.BONDING_OPT % (bond, 'mode')
    paths = (p for p in iglob(sysfs_options.BONDING_OPT % (bond, '*'))
             if p != bond_mode_path)
    opts = {}

    for path in paths:
        elements = sysfs_options.bond_opts_read_elements(path)
        if len(elements) == 2:
            opts[os.path.basename(path)] = \
                _bond_opts_name2numeric_scan(path)
    return opts


def _bond_opts_name2numeric_scan(opt_path):
    vals = {}
    with io.open(opt_path, 'wb', buffering=0) as opt_file:
        for numeric_val in range(32):
            name, numeric = _bond_opts_name2numeric_getval(opt_path, opt_file,
                                                           numeric_val)
            if name is None:
                break

            vals[name] = numeric

    return vals


def _bond_opts_name2numeric_getval(opt_path, opt_write_file, numeric_val):
    try:
        opt_write_file.write(str(numeric_val).encode('utf8'))
    except IOError as e:
        if e.errno in (errno.EINVAL, errno.EPERM, errno.EACCES):
            return None, None
        else:
            e.filename = "opt[%s], numeric_val[%s]" % (opt_path, numeric_val)
            raise

    return sysfs_options.bond_opts_read_elements(opt_path)
