"""Exceptions raised by the bhyve compute driver."""

from nova.i18n import _

from nova import exception


class BhyveDriverError(exception.NovaException):
    """A bhyve driver operation failed."""

    msg_fmt = _("bhyve driver error: %(reason)s")


class ZFSCommandError(BhyveDriverError):
    """A zfs(8) invocation failed."""

    msg_fmt = _("zfs %(command)s failed for %(dataset)s: %(reason)s")


class MachineError(BhyveDriverError):
    """A machine (hypervisor mechanism) operation failed."""

    msg_fmt = _("bhyve machine error for %(uuid)s: %(reason)s")
