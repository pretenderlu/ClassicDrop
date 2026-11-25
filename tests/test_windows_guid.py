import json
import os
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_classicdrop import GUID, mock_device
import system
from library import _device_guids, _ipod_usb_instance

_windows_disk_ids = system._windows_disk_ids


# Exercise the shipped PowerShell query against a synthetic PnP tree.
WINDOWS_FIXTURE = r"""
$chain = ConvertFrom-Json $env:CLASSICDROP_TEST_PNP_CHAIN
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($ClassName -ne 'Win32_LogicalDisk' -or $Filter -ne "DeviceID='Q:'") { throw 'wrong volume' }
    return 'Q:'
}
function Get-CimAssociatedInstance {
    param($InputObject, $Association)
    if ($InputObject -eq 'Q:' -and $Association -eq 'Win32_LogicalDiskToPartition') { return 'partition' }
    if ($InputObject -ne 'partition' -or $Association -ne 'Win32_DiskDriveToDiskPartition') { throw 'wrong disk' }
    return [pscustomobject]@{SerialNumber = '1122334455667788'; PNPDeviceID = $chain[0]}
}
function Get-PnpDeviceProperty {
    param($InstanceId, $KeyName, $ErrorAction)
    $position = [array]::IndexOf($chain, $InstanceId)
    if ($position -lt 0 -or $KeyName -ne 'DEVPKEY_Device_Parent') { throw 'wrong branch' }
    return [pscustomobject]@{Data = $chain[($position + 1)]}
}
"""


@unittest.skipUnless(system.WINDOWS, "Windows PnP tree and eject APIs")
class WindowsGuidTests(unittest.TestCase):
    def test_guid_from_own_physical_usb_device(self):
        disk = r"USBSTOR\DISK&VEN_APPLE&PROD_IPOD\GENERIC&0"
        interface = r"USB\VID_05AC&PID_1261&MI_00\7&ABC&0"
        apple = "USB\\VID_05AC&PID_1261\\" + GUID.hex()
        invalid_serial = r"USB\VID_05AC&PID_1261\INVALID"
        legacy = bytes.fromhex("1122334455667788")
        cases = (
            ([disk, interface, apple], [legacy, GUID], apple),
            ([disk, r"USB\VID_1234&PID_5678\DISK", apple], [legacy], None),
            ([disk, interface, invalid_serial, apple], [legacy], invalid_serial),
            ([disk, interface], [legacy], None),
        )
        run = subprocess.run
        for chain, expected, ejectable in cases:
            with self.subTest(chain=chain):
                def simulated(command, **kwargs):
                    result = run([*command[:-1], WINDOWS_FIXTURE + command[-1]],
                                 env={**os.environ, "CLASSICDROP_TEST_PNP_CHAIN": json.dumps(chain)}, **kwargs)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result
                with patch("system.subprocess.run", side_effect=simulated):
                    ids = _windows_disk_ids("Q:")
                self.assertEqual(list(filter(None, _device_guids(Path(__file__).parent, ids))), expected)
                self.assertEqual(_ipod_usb_instance(ids), ejectable)

    def test_parent_timeout_keeps_disk_identifier(self):
        timeout = subprocess.TimeoutExpired("powershell", 12, output=(GUID.hex() + "\r\n").encode("ascii"))
        with patch("system.subprocess.run", side_effect=timeout):
            self.assertEqual(list(_device_guids(Path(__file__).parent, _windows_disk_ids("Q:"))), [GUID])

    def test_eject_verifies_disk_and_propagates_windows_veto(self):
        instance = "USB\\VID_05AC&PID_1261\\" + GUID.hex()
        with TemporaryDirectory() as tmp:
            copy = mock_device(Path(tmp) / "iPod")
            dev = replace(copy, root=Path("Q:/"), guid=None, usb_instance=instance)
            with patch("system._windows_disk_ids", return_value=(instance,)) as ids, patch("system.ctypes.WinDLL") as dll:
                with self.assertRaisesRegex(ValueError, "USB"):
                    copy.eject()
                ids.assert_not_called()
                foreign = r"USB\VID_0123&PID_1261\OTHER"
                for different in ((), (foreign,), (instance + "1",)):
                    ids.return_value = different
                    with self.assertRaisesRegex(OSError, "断开或改变"):
                        dev.eject()
                dll.assert_not_called()
                ids.return_value = (instance,)
                api = dll.return_value
                def locate(node, chosen, flags):
                    self.assertEqual((chosen, flags), (instance, 0))
                    node._obj.value = 22
                    return 0
                def veto(node, reason, name, size, flags):
                    self.assertEqual((node.value, size, flags), (22, 260, 0))
                    name.value = "Explorer"
                    return 0x17
                api.CM_Locate_DevNodeW.side_effect = locate
                api.CM_Request_Device_EjectW.side_effect = veto
                with self.assertRaisesRegex(OSError, "Explorer"):
                    dev.eject()
                api.CM_Request_Device_EjectW.side_effect = None
                api.CM_Request_Device_EjectW.return_value = 0
                dev.eject()
                ids.assert_called_with("Q:")
                self.assertEqual(api.CM_Request_Device_EjectW.call_count, 2)
                api.CM_Locate_DevNodeW.side_effect = None
                api.CM_Locate_DevNodeW.return_value = 13
                with self.assertRaisesRegex(OSError, "找不到"):
                    dev.eject()
                self.assertEqual(api.CM_Request_Device_EjectW.call_count, 2)
