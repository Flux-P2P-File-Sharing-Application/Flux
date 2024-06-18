import select
import socket
import threading

import msgpack

HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
SERVER_IP = "127.0.0.1"
SERVER_PORT = 1234
ENCODING_FORMAT = "utf-8"

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server_socket.bind((SERVER_IP, SERVER_PORT))
server_socket.listen()

active_sockets = [server_socket]
# username -> address: (IP, PORT)
# Mapping of usernames to addresses (IP, PORT)
clients: dict[str, tuple[str, int]] = {}

def receive_message(client_socket: socket.socket) -> dict[str, str | bytes]:
    message_type = client_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    if not len(message_type):
        raise Exception(msg="Client closed the connection")
    elif message_type not in ("n", "r"):
        raise Exception(msg="Invalid message type in header")
    else:
        message_length = int(client_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
        return {"type": message_type, "username": client_socket.recv(message_length)}


def handle_client_socket(notified_socket: socket.socket) -> None:
    global clients
    global active_sockets
    if notified_socket == server_socket:
        client_socket, client_address = server_socket.accept()
        try:
            user_data = receive_message(client_socket)
            if user_data["type"] == "n":
                active_sockets.append(client_socket)
                clients[user_data["username"]] = client_address
                print(
                    (
                        "Accepted new connection from"
                        f" {client_address[0]}:{client_address[1]}"
                        f" username: {user_data['username'].decode(ENCODING_FORMAT)}"
                    )
                )
            else:
                print(f"Bad request from {client_address}")
                return
        except Exception as e:
            print(f"Exception: {e.msg}")
            return
    else:
        try:
            request = receive_message(notified_socket)
            if request["type"] == "r":
                response_data = clients.get(request["username"])
                if response_data is not None:
                    data: bytes = msgpack.packb(response_data)
                    notified_socket.send(data)
                else:
                    print(f"Username {request['username']} not found")
                    return
            else:
                print(f"Bad request from {notified_socket.getpeername()}")
                return
        except Exception as e:
            active_sockets.remove(notified_socket)
            for username, address in clients.items():
                if address == notified_socket.getpeername():
                    del clients[username]
                    break
            print(f"Exception: {e.msg}")
            return


while True:
    readable_sockets: list[socket.socket]
    exception_sockets: list[socket.socket]

    readable_sockets, _, exception_sockets = select.select(
        active_sockets, [], active_sockets
    )
    for notified_socket in readable_sockets:
        # threads
        thread = threading.Thread(target=handle_client_socket, args=(notified_socket,))
        thread.start()

    for notified_socket in exception_sockets:
        active_sockets.remove(notified_socket)
        for username, address in clients.items():
            if address == notified_socket.getpeername():
                del clients[username]
                break
