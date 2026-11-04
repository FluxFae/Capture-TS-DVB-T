"""Device selection tests — M2/M3.

The WinTV-soloHD is a hybrid receiver: ``tslsdvb`` reports index 0 as
``DVB-T, DVB-T2`` and index 1 as ``DVB-C, DVB-C/C``. The form used to mark a
device ``selected`` from a substring check, so two DVB-T-capable receivers both
got a ``selected`` attribute and the browser picked the last one; the SYSTEM
select was hardcoded to DVB-T/DVB-T2, so the DVB-C tuner could never be used
coherently; and any adapter the user submitted (including the ``-1``
"No devices found" placeholder) was passed straight to ``tsp``.

The rules these tests pin:

- exactly one device is chosen deterministically, preferring the verified
  DVB-T path;
- a submitted adapter must exist;
- a submitted delivery system must be one the chosen device actually reports,
  so ``tsp -a 1 --delivery-system DVB-T`` (a hard failure on the DVB-C tuner)
  is rejected before the process is spawned.
"""

import unittest

from app.services import devices

# Verbatim from `tslsdvb` on the development machine.
DVB_T_DEVICE = devices.DVBDevice(
    index=0,
    name="Hauppauge WinTV-soloHD DVBT Tuner",
    delivery_systems=["DVB-T", "DVB-T2"],
)
DVB_C_DEVICE = devices.DVBDevice(
    index=1,
    name="Hauppauge WinTV-soloHD DVBC Tuner",
    delivery_systems=["DVB-C", "DVB-C/C"],
)


class DefaultDeviceTest(unittest.TestCase):
    def test_prefers_the_dvb_t_device(self):
        chosen = devices.default_device([DVB_C_DEVICE, DVB_T_DEVICE])

        if chosen is None:
            self.fail("a DVB-T capable device must be selected")
        self.assertEqual(chosen.index, 0)

    def test_single_device_is_chosen(self):
        chosen = devices.default_device([DVB_T_DEVICE])

        if chosen is None:
            self.fail("the only reported device must be selected")
        self.assertEqual(chosen.index, 0)

    def test_falls_back_to_the_first_device_without_dvb_t(self):
        chosen = devices.default_device([DVB_C_DEVICE])

        if chosen is None:
            self.fail("the only reported device must be selected")
        self.assertEqual(chosen.index, 1)

    def test_no_devices_yields_nothing(self):
        self.assertIsNone(devices.default_device([]))


class DefaultDeliverySystemTest(unittest.TestCase):
    def test_dvb_t_is_the_default_on_the_verified_device(self):
        self.assertEqual(devices.default_delivery_system(DVB_T_DEVICE), "DVB-T")

    def test_first_reported_system_is_the_default_otherwise(self):
        self.assertEqual(devices.default_delivery_system(DVB_C_DEVICE), "DVB-C")

    def test_no_device_yields_no_default(self):
        self.assertEqual(devices.default_delivery_system(None), "")


class AvailableDeliverySystemsTest(unittest.TestCase):
    def test_union_follows_the_device_order(self):
        self.assertEqual(
            devices.available_delivery_systems([DVB_T_DEVICE, DVB_C_DEVICE]),
            ["DVB-T", "DVB-T2", "DVB-C", "DVB-C/C"],
        )

    def test_duplicates_across_devices_are_collapsed(self):
        twin = devices.DVBDevice(index=2, name="second DVB-T stick", delivery_systems=["DVB-T"])

        self.assertEqual(
            devices.available_delivery_systems([DVB_T_DEVICE, twin]),
            ["DVB-T", "DVB-T2"],
        )

    def test_no_devices_yield_no_options(self):
        self.assertEqual(devices.available_delivery_systems([]), [])


class ResolveDeviceTest(unittest.TestCase):
    def setUp(self):
        self.devices = [DVB_T_DEVICE, DVB_C_DEVICE]

    def test_explicit_dvb_c_device_and_system_are_accepted(self):
        chosen = devices.resolve_device(self.devices, adapter=1, delivery_system="DVB-C")

        self.assertEqual(chosen.index, 1)

    def test_second_system_of_a_device_is_accepted(self):
        chosen = devices.resolve_device(self.devices, adapter=0, delivery_system="DVB-T2")

        self.assertEqual(chosen.index, 0)

    def test_unknown_adapter_is_rejected(self):
        with self.assertRaises(devices.DeviceSelectionError) as caught:
            devices.resolve_device(self.devices, adapter=5, delivery_system="DVB-T")

        self.assertIn("5", str(caught.exception))

    def test_placeholder_adapter_is_rejected(self):
        with self.assertRaises(devices.DeviceSelectionError):
            devices.resolve_device(self.devices, adapter=-1, delivery_system="DVB-T")

    def test_system_the_device_does_not_report_is_rejected(self):
        with self.assertRaises(devices.DeviceSelectionError) as caught:
            devices.resolve_device(self.devices, adapter=0, delivery_system="DVB-C")

        message = str(caught.exception)
        self.assertIn("DVB-C", message)
        self.assertIn("DVB-T", message)

    def test_missing_device_list_is_rejected(self):
        with self.assertRaises(devices.DeviceSelectionError):
            devices.resolve_device([], adapter=0, delivery_system="DVB-T")


if __name__ == "__main__":
    unittest.main()
