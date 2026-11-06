from app.hub import AppClient, hub
from app.models import Role, User
from app.twin.devices import GOOD_WALK, SLOW_DUAL_WALK, Message, VirtualDevices, VirtualSocket

FakeSocket = VirtualSocket
FakeDevices = VirtualDevices

__all__ = ["GOOD_WALK", "SLOW_DUAL_WALK", "FakeDevices", "FakeSocket", "Message", "connect_app"]


def connect_app(user: User) -> FakeSocket:
    socket = FakeSocket()
    hub.apps.add(AppClient(socket, user.id, Role(user.role)))  # type: ignore[arg-type]
    return socket
