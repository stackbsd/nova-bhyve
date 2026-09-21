# nova-bhyve

An OpenStack Nova compute driver that runs instances as [bhyve](https://bhyve.org/) virtual machines on FreeBSD.

## Status

### Features

- Direct VM management through `bhyve`/`bhyvectl`
- Instance root disks on ZFS
- Supports booting from qcow2 and raw Glance images
- Glance images cached as zvols with zero copy root disk cloning
- VNC console integration through `nova-novncproxy`
- Cold attach of cinder volumes
- Cold attach of network interfaces
- Instance snapshot and upload to Glance

### Roadmap

- Cold migration between hosts (currently only supports same-host migration i.e. instance resize)
- Automatically remove cached images once last instance is gone
- Support for additional ephemeral/swap disks

### Limitations

- Live migration is not supported yet by bhyve, so is not implemented in this driver

## Host requirements

`zfs allow` delegation is needed for the dataset holding images and instances.

A `devfs.rules` entry is needed to grant the group running Nova read/write access to the zvol device nodes.

You must configure `volmode=dev` on the dataset so that the host does not publish guest partition tables into its own `/dev`.

## Configuration

Example configuration for Nova:

```
[DEFAULT]
compute_driver = bhyve.driver.BhyveDriver
allow_resize_to_same_host = True

[bhyve]
zfs_dataset = zroot/nova
```
