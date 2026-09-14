Place MGX ARC firmware image files here for one-click flashing from the GUI.

BMC images:
  mgxa-2606-24.00.image                      — BMC w/ ERoT (Redfish multipart update)
  MGX_ARC_26062401_dev_debug.fwpkg           — BMC w/o ERoT via .fwpkg (Redfish multipart)
  MGX_ARC_26062401_dev_debug.signed-apimage.bin — BMC w/o ERoT via .bin (SCP to /run/initramfs/image-bmc + reboot)

SMR / FPGA image:
  FPGA_arc_0v0C_signed-apimage.bin   — SMR target version 0V0C (Redfish → FW_FPGA_0)

SBIOS / UEFI image:
  SBIOS_P4180_PG535_P4180_SKU_893_NC00_02.06.06_rel_prod.fwpkg   — SBIOS 02.06.06 (Redfish → FW_CPU_0)

ERoT image:
  cec1736-ecfw-01.04.0037.0000-nc00-rel-prod.bin   — ERoT 01.04.0037.0000_nc00 (Redfish → FW_ERoT_CPU_0)

FML bundle (.fwpkg):
  nvfw_Grace-CPU-P4180_0017_260603.1.1_custom_prod-signed.fwpkg

ConnectX8 (.fwpkg) — latest 40.97.5444:
  fw-ConnectX8-PK-40.97.5444.fwpkg   — PK / MGX ARC PK boards
  fw-ConnectX8-QP-40.97.5444.fwpkg   — QP / MGX ARC QP boards

Copy from Downloads:
  PK: fw-ConnectX8-rel-40_97_5444-cx8_P4180_MRG_ARC_PK_Ax-...-NVD0000000141.fwpkg
      → firmware_images/fw-ConnectX8-PK-40.97.5444.fwpkg
  QP: fw-ConnectX8-rel-40_97_5444-cx8_P4180_MGX_ARC_QP_Ax-...-NVD0000000138.fwpkg
      → firmware_images/fw-ConnectX8-QP-40.97.5444.fwpkg
  SMR: FPGA_arc_0v0C_signed-apimage (3).bin
      → firmware_images/FPGA_arc_0v0C_signed-apimage.bin
  SBIOS: SBIOS_P4180_PG535_P4180_SKU_893_NC00_02.06.06_rel_prod (1).fwpkg
      → firmware_images/SBIOS_P4180_PG535_P4180_SKU_893_NC00_02.06.06_rel_prod.fwpkg

You can also upload images directly in the Flash tab.
