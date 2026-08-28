"""HTTP routes.

Everything this service serves lives under :data:`MOUNT_PATH`, in every
deployment shape — behind the TLS proxy the gateway owns the root of the origin,
and ``/api`` and ``/auth`` there are *its* management API and *its* login
callback. A chat that answered on those paths would work on its own port and
collide the moment it was put behind the proxy, which is the kind of bug that
only appears in the deployment that matters.

The console solved the same problem the same way, at ``/console``.
"""

MOUNT_PATH = "/chat"
