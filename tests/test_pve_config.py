from app.services.pve_config import parse_disks, total_disk_bytes


def test_parse_disks_qemu_mixed_buses():
    config = {
        "scsi0": "local-lvm:vm-100-disk-0,size=20G,ssd=1,discard=on",
        "scsi1": "bulk-hdd:vm-100-disk-1,size=24G",
        "efidisk0": "local-lvm:vm-100-disk-2,efitype=4m,pre-enrolled-keys=1,size=528K",
        "ide2": "local:iso/debian.iso,media=cdrom",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        "unused0": "old-store:vm-100-disk-9",
        "memory": "4096",
    }
    disks = parse_disks(config)
    # cdrom, net, unused and scalar keys are dropped; three data disks remain.
    assert [d["dev"] for d in disks] == ["scsi0", "scsi1", "efidisk0"]
    assert disks[0] == {"dev": "scsi0", "storage": "local-lvm", "size_bytes": 20 * 1024**3}
    assert disks[1]["storage"] == "bulk-hdd"
    assert disks[1]["size_bytes"] == 24 * 1024**3
    assert disks[2]["size_bytes"] == 528 * 1024


def test_parse_disks_orders_fast_buses_first():
    config = {
        "ide0": "store:vm-1-disk-3,size=1G",
        "virtio0": "store:vm-1-disk-0,size=1G",
        "scsi0": "store:vm-1-disk-1,size=1G",
        "sata0": "store:vm-1-disk-2,size=1G",
    }
    assert [d["dev"] for d in parse_disks(config)] == ["virtio0", "scsi0", "sata0", "ide0"]


def test_parse_disks_lxc_rootfs_and_mounts():
    config = {
        "rootfs": "local-lvm:subvol-200-disk-0,size=8G",
        "mp0": "bulk-hdd:subvol-200-disk-1,size=500G,mp=/data",
    }
    disks = parse_disks(config)
    assert [d["dev"] for d in disks] == ["rootfs", "mp0"]
    assert total_disk_bytes(disks) == 8 * 1024**3 + 500 * 1024**3


def test_parse_disks_skips_sizeless_and_none():
    config = {
        "scsi0": "none,media=cdrom",
        "tpmstate0": "local-lvm:vm-1-disk-5,size=4M,version=v2.0",
        "ide2": "cloudinit",
    }
    disks = parse_disks(config)
    assert [d["dev"] for d in disks] == ["tpmstate0"]
    assert disks[0]["size_bytes"] == 4 * 1024**2


def test_parse_disks_handles_empty():
    assert parse_disks(None) == []
    assert parse_disks({}) == []
    assert total_disk_bytes([]) == 0
