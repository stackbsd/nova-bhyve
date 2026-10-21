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
- Resize on the same host, and cold migration between hosts using `zfs send` over ssh

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
# migration_ssh_config = /usr/local/etc/nova/ssh_config
# migration_staging_path = /var/db/nova-migrate/instances
```

### Migration between hosts

To migrate between hosts, the nova user needs `send` added to its `zfs allow` delegation and an ssh key.
See the `migration_ssh_config` configuration opt. 

The user on the receiving host needs `create,destroy,receive` delegated on the dataset and write access to `migration_staging_path`.

Additionally you can lock down this user to only execute the receive command, for example (in `authorized_keys`):

```
command="nova-bhyve-receive --config-file /path/to/nova.conf",restrict ssh-ed25519 AAAA...
```