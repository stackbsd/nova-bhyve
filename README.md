# nova-bhyve

An OpenStack Nova compute driver that runs instances as [bhyve](https://bhyve.org/) virtual machines on FreeBSD.

## Status

### Features

- Direct VM management through `bhyve`/`bhyvectl`
- Instance root disks on ZFS
- Ephemeral and swap disks as sparse zvols
- Supports booting from qcow2 and raw Glance images
- Glance images cached as zvols with zero copy root disk cloning
- VNC console integration through `nova-novncproxy`
- Config drive for instance metadata and user data
- Cold attach of cinder volumes, as local zvols or remote over iSCSI (using [os-brick-freebsd](https://github.com/stackbsd/os-brick-freebsd))
- Cold attach of network interfaces
- Instance snapshot and upload to Glance
- Nova image cache manager support to prune cache when no longer needed

### Roadmap

- Cold migration between hosts (currently only supports same-host migration i.e. instance resize)

### Limitations

- Live migration is not supported yet by bhyve, so is not implemented in this driver
- Ephemeral and swap disks are attached unformatted, the guest has to create the filesystem or swap area
- A volume extended while attached shows its new size to the guest after the next start

## Host requirements

`zfs allow` delegation is needed for the dataset holding images and instances.

A `devfs.rules` entry is needed to grant the group running Nova read/write access to the zvol device nodes.

You must configure `volmode=dev` on the dataset so that the host does not publish guest partition tables into its own `/dev`.

To remote attach cinder volumes, the nova host needs `iscsid` running and the os-brick-freebsd package installed.

## Configuration

Example configuration for Nova:

```
[DEFAULT]
compute_driver = bhyve.driver.BhyveDriver
allow_resize_to_same_host = True

[bhyve]
zfs_dataset = zroot/nova
```
