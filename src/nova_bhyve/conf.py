"""Configuration options for the bhyve compute driver."""

from oslo_config import cfg

bhyve_group = cfg.OptGroup(
    "bhyve",
    title="bhyve driver options",
    help="Options for the bhyve compute driver.",
)

bhyve_opts = [
    cfg.StrOpt(
        "zfs_dataset",
        help="Dataset under which the driver creates its zvols, e.g. "
        '"zroot/nova". Required: the driver refuses to start if this '
        "is unset or if the dataset (or its images/ and instances/ "
        "children) is missing. The nova user must hold ZFS delegated "
        "administration on it.",
    ),
    cfg.StrOpt(
        "mechanism",
        default="direct",
        choices=("direct",),
        help="Mechansim for interacting with bhyve.",
    ),
    cfg.StrOpt(
        "bootrom_path",
        default="/usr/local/share/edk2-bhyve/BHYVE_UEFI_CODE.fd",
        help="UEFI firmware image used for bhyve bootrom.",
    ),
    cfg.StrOpt(
        "nvram_template",
        default="/usr/local/share/edk2-bhyve/BHYVE_UEFI_VARS.fd",
        help="Template for instance UEFI variables."
    ),
    cfg.IntOpt(
        "destroy_timeout",
        default=10,
        min=1,
        help="Seconds to wait for a guest to disappear after a hard stop "
        "before giving up and reclaiming its resources anyway."
    ),
    cfg.StrOpt(
        "image_scratch_dir",
        default="$state_path/bhyve-scratch",
        help="Directory for spool files while converting qcow2 to raw.",
    ),
    cfg.StrOpt(
        "snapshot_image_format",
        default="qcow2",
        choices=("qcow2", "raw"),
        help="Disk format for instance snapshots uploaded to Glance.\n"
        "\n"
        "qcow2 is the default so that snapshots are sparse.",
    ),
    cfg.BoolOpt(
        "ignore_unimplemented_msr",
        default=False,
        help="Might help with running bhyve nested in a KVM guest.",
    ),
    cfg.IntOpt(
        "shutdown_timeout_graceful",
        default=30,
        min=0,
        help="Seconds to wait for an ACPI shutdown to complete before "
        "destroying the domain.",
    ),
]

CONF = cfg.CONF
CONF.register_group(bhyve_group)
CONF.register_opts(bhyve_opts, group=bhyve_group)


def list_opts():
    """Return the driver options for oslo-config-generator."""
    return [(bhyve_group, bhyve_opts)]
