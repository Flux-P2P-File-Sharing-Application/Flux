import select
import socket
import logging
import sys
import os

import msgpack

from ..exceptions import ExceptionCode, RequestException
from ..headers import HeaderCode

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
HEADER_TYPE_LEN = 1
HEADER_MSG_LEN = 7
SERVER_IP = socket.gethostbyname(socket.gethostname())
SERVER_PORT = 1234
ENCODING_FORMAT = "utf-8"
FMT = ENCODING_FORMAT

# Ensure the logs directory exists
log_directory = "./logs"
os.makedirs(log_directory, exist_ok=True)

log_filename = f"{log_directory}/server_{SERVER_IP}.log"
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler(sys.stdout),
    ],
)

# Create a TCP/IP socket
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Allow reusing the same address
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

# Bind the socket to the server's IP address and port
server_socket.bind((SERVER_IP, SERVER_PORT))

# Listen for incoming connections
server_socket.listen(5)

# List of active sockets including the server socket
active_sockets = [server_socket]
# Mapping of usernames to addresses (IP, PORT)
clients: dict[str, str] = {}

# Function to receive messages from the client
def receive_msg(client_socket: socket.socket) -> dict[str, bytes | HeaderCode]:
    """
    Receive a message from the client socket.
    """
    # Receive message type
    message_type = client_socket.recv(HEADER_TYPE_LEN).decode(FMT)
    if not len(message_type):
        # Handle client disconnect
        raise RequestException(
            msg=f"Client at {client_socket.getpeername()} closed the connection",
            code=ExceptionCode.DISCONNECT,
        )
    elif message_type not in [
        HeaderCode.NEW_CONNECTION.value,
        HeaderCode.REQUEST_UNAME.value,
        HeaderCode.LOOKUP_ADDRESS.value,
    ]:
        # Handle invalid message type
        logging.error(msg=f"Received message type {message_type}")
        raise RequestException(
            msg="Invalid message type in header",
            code=ExceptionCode.INVALID_HEADER,
        )
    else:
        # Receive message length and query data
        message_len = int(client_socket.recv(HEADER_MSG_LEN).decode(FMT))
        query = client_socket.recv(message_len)
        logging.debug(
            msg=f"Received packet: TYPE {message_type} QUERY {query} from {client_socket.getpeername()}"
        )
        return {"type": HeaderCode(message_type), "query": query}

def read_handler(notified_socket: socket.socket) -> None:
    """
    Handle incoming requests from clients.
    """
    global clients
    global active_sockets
    logging.info(f"CLIENTS {clients}")
    
    # Accept new connections
    if notified_socket == server_socket:
        client_socket, client_address = server_socket.accept()
        try:
            # Process initial message from new client
            user_data = receive_msg(client_socket)
            username = user_data["query"].decode(FMT)
            active_sockets.append(client_socket)

            if user_data["type"] == HeaderCode.NEW_CONNECTION:
                address = clients.get(username)
                logging.debug(
                    msg=f"Registration request for username {username} from address {client_address}"
                )
                # Check if the username is already registered
                if address is None:
                    clients[username] = client_address[0]
                    logging.debug(
                        msg=(
                            "Accepted new connection from"
                            f" {client_address[0]}:{client_address[1]}"
                            f" username: {username}"
                        ),
                    )
                    client_socket.send(f"{HeaderCode.NEW_CONNECTION.value}".encode(FMT))
                # Check if the address is already registered
                else:
                    if address != client_address[0]:
                        raise RequestException(
                            msg=f"User with username {address} already exists",
                            code=ExceptionCode.USER_EXISTS,
                        )
                    else:
                        raise RequestException(
                            msg="Cannot re-register user for same address",
                            code=ExceptionCode.BAD_REQUEST,
                        )
            else:
                raise RequestException(
                    msg=f"Bad request from {client_address}",
                    code=ExceptionCode.BAD_REQUEST,
                )
        except RequestException as e:
            # Handle exceptions and send error responses
            if e.code != ExceptionCode.DISCONNECT:
                data: bytes = msgpack.packb(
                    e, default=RequestException.to_dict, use_bin_type=True
                )
                header = f"{HeaderCode.ERROR.value}{len(data):<{HEADER_MSG_LEN}}".encode(FMT)
                client_socket.send(header + data)

            # Close the client socket if an exception code is DISCONNECT
            for key, value in clients.items():
                if value == client_address[0]:
                    del clients[key]
                    break
            else:
                logging.debug(msg=f"Username for IP {client_address[0]} not found")

            logging.error(msg=e.msg)
            return
    else:
        try:
            # Handle requests from existing clients
            request = receive_msg(notified_socket)
            if request["type"] == HeaderCode.REQUEST_UNAME:
                # Handle request to retrieve client address
                response_data = clients.get(request["query"].decode(FMT))
                if response_data is not None:
                    # if the address is not the same as the client address
                    if response_data != notified_socket.getpeername()[0]:
                        logging.debug(msg=f"Valid request: {response_data}")
                        data: bytes = response_data.encode(FMT)
                        header = f"{HeaderCode.REQUEST_UNAME.value}{len(data):<{HEADER_MSG_LEN}}".encode(FMT)
                        notified_socket.send(header + data)
                    else:
                        raise RequestException(
                            msg="Cannot query for user having the same address",
                            code=ExceptionCode.BAD_REQUEST,
                        )
                else:
                    raise RequestException(
                        msg=f"Username {request['query'].decode(FMT)} not found",
                        code=ExceptionCode.NOT_FOUND,
                    )
            elif request["type"] == HeaderCode.LOOKUP_ADDRESS:
                # Handle request to lookup clients by IP address
                lookup_address = request["query"].decode(FMT)
                if lookup_address != notified_socket.getpeername()[0]:
                    for key, value in clients.items():
                        if value == lookup_address:
                            username = key.encode(FMT)
                            header = f"{HeaderCode.LOOKUP_ADDRESS.value}{len(username):<{HEADER_MSG_LEN}}".encode(FMT)
                            notified_socket.send(header + username)
                            break
                    else:
                        raise RequestException(
                            msg=f"Username for {lookup_address} not found",
                            code=ExceptionCode.NOT_FOUND,
                        )
                else:
                    raise RequestException(
                        msg="Cannot query for user having the same address",
                        code=ExceptionCode.BAD_REQUEST,
                    )
            # Handle registration requests from clients
            elif request["type"] == HeaderCode.NEW_CONNECTION:
                username = request["query"].decode(FMT)
                address = clients.get(username)
                client_address = notified_socket.getpeername()
                logging.debug(
                    msg=f"Registration request for username {username} from address {client_address}"
                )
                if address is None:
                    clients[username] = client_address[0]
                    logging.debug(
                        msg=(
                            "Accepted new connection from"
                            f" {client_address[0]}:{client_address[1]}"
                            f" username: {username}"
                        ),
                    )
                    notified_socket.send(f"{HeaderCode.NEW_CONNECTION.value}".encode(FMT))
                else:
                    if address != client_address[0]:
                        raise RequestException(
                            msg=f"User with username {address} already exists",
                            code=ExceptionCode.USER_EXISTS,
                        )
                    else:
                        raise RequestException(
                            msg="Cannot re-register user for same address",
                            code=ExceptionCode.BAD_REQUEST,
                        )
            else:
                raise RequestException(
                    msg=f"Bad request from {notified_socket.getpeername()}",
                    code=ExceptionCode.BAD_REQUEST,
                )
        except TypeError as e:
            logging.error(msg=e)
            sys.exit(0)
        except RequestException as e:
            # Handle exceptions and send error responses
            if e.code == ExceptionCode.DISCONNECT:
                try:
                    active_sockets.remove(notified_socket)
                    # Remove client from clients dictionary
                    address = notified_socket.getpeername()[0]
                    for key, value in clients.items():
                        if value == address:
                            del clients[key]
                            break
                    else:
                        logging.debug(msg=f"Username for IP {address} not found")
                except ValueError:
                    logging.info("already removed")
            else:
                data: bytes = msgpack.packb(
                    e, default=RequestException.to_dict, use_bin_type=True
                )
                header = f"{HeaderCode.ERROR.value}{len(data):<{HEADER_MSG_LEN}}".encode(FMT)
                notified_socket.send(header + data)
            logging.error(msg=f"Exception: {e.msg}")
            return

while True:
    # Lists to hold sockets that are ready for reading or have an error
    readable_sockets: list[socket.socket]
    exception_sockets: list[socket.socket]

    # Use select to wait for I/O readiness
    readable_sockets, _, exception_sockets = select.select(
        active_sockets, [], active_sockets
    )

    # Handle all readable sockets
    for notified_socket in readable_sockets:
        read_handler(notified_socket)
