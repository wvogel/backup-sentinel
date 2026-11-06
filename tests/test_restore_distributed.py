from app.services.restore_server import _parse_config_disks, _size_token_bytes

CONFIG = """\
boot: order=scsi0;scsi1
cores: 4
memory: 8192
scsi0: fast-ssd:vm-100-disk-0,size=20G,ssd=1,discard=on
scsi1: bulk-hdd:vm-100-disk-1,size=200G
ide2: local:iso/debian.iso,media=cdrom
efidisk0: fast-ssd:vm-100-disk-2,efitype=4m,pre-enrolled-keys=1,size=528K
net0: virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0
[previous-snap]
scsi0: fast-ssd:vm-100-disk-0,size=10G
"""


def test_parse_config_disks_extracts_data_disks_only():
    disks = _parse_config_disks(CONFIG)
    assert set(disks) == {"scsi0", "scsi1", "efidisk0"}  # cdrom, net, scalars out
    assert disks["scsi0"]["volid"] == "fast-ssd:vm-100-disk-0"
    assert disks["scsi0"]["size_token"] == "20G"
    assert disks["scsi0"]["opts"] == ",size=20G,ssd=1,discard=on"


def test_parse_config_disks_stops_before_snapshot_section():
    # The [previous-snap] section's scsi0 (10G) must not override the live 20G.
    disks = _parse_config_disks(CONFIG)
    assert disks["scsi0"]["size_token"] == "20G"


def test_rebuilding_a_disk_line_swaps_only_the_volid():
    disks = _parse_config_disks(CONFIG)
    rebuilt = f"scsi1: bulk-nvme:vm-9000-disk-0{disks['scsi1']['opts']}"
    assert rebuilt == "scsi1: bulk-nvme:vm-9000-disk-0,size=200G"


def test_size_token_bytes_binary_units():
    assert _size_token_bytes("20G") == 20 * 1024**3
    assert _size_token_bytes("528K") == 528 * 1024
    assert _size_token_bytes("4M") == 4 * 1024**2
    assert _size_token_bytes("") is None
    assert _size_token_bytes(None) is None
