import select
import socket
import logging
import sys
import os

import msgpack

from exceptions import ExceptionCode, RequestException

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
SERVER_IP = "127.0.0.1"
SERVER_PORT = 1234
ENCODING_FORMAT = "utf-8"

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
def receive_message(client_socket: socket.socket) -> dict[str, str | bytes]:
    """
    Receive a message from the client socket.
    """
    # Receive message type
    message_type = client_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    if not len(message_type):
        # Handle client disconnect
        raise RequestException(
            msg=f"Client at {client_socket.getpeername()} closed the connection",
            code=ExceptionCode.DISCONNECT,
        )
    elif message_type not in ("n", "r", "l"):
        # Handle invalid message type
        logging.error(f"Received message type {message_type}")
        raise RequestException(
            msg="Invalid message type in header",
            code=ExceptionCode.INVALID_HEADER,
        )
    else:
        # Receive message length and query data
        message_length = int(client_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
        query = client_socket.recv(message_length)
        #here query is the username
        logging.debug(f"Received packet: TYPE {message_type} QUERY {query} from {client_socket.getpeername()}")
        return {"type": message_type, "query": query}

# Function to handle client sockets
def handle_client_socket(notified_socket: socket.socket) -> None:
    """
    Handle a client socket that has data to be processed.
    """
    global clients
    global active_sockets
    logging.info(f"CLIENTS {clients}")
    
    # Accept new connections
    if notified_socket == server_socket:
        client_socket, client_address = server_socket.accept()
        try:
            # Process initial message from new client
            user_data = receive_message(client_socket)
            if user_data["type"] == "n":
                active_sockets.append(client_socket)
                clients[user_data["query"].decode(ENCODING_FORMAT)] = client_address[0]
                logging.log(
                    level=logging.DEBUG,
                    msg=(
                        "Accepted new connection from"
                        f" {client_address[0]}:{client_address[1]}"
                        f" username: {user_data['query'].decode(ENCODING_FORMAT)}"
                    ),
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
                header = f"e{len(data):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                client_socket.send(header + data)
            logging.log(level=logging.ERROR, msg=f"Exception: {e.msg}")
            return
    else:
        try:
            # Handle requests from existing clients
            request = receive_message(notified_socket)
            if request["type"] == "r":
                # Handle request to retrieve client address
                response_data = clients.get(request["query"].decode(ENCODING_FORMAT))
                if response_data is not None:
                    logging.log(
                        level=logging.DEBUG,
                        msg=f"Valid request: {response_data}",
                    )
                    data: bytes = response_data.encode(ENCODING_FORMAT)
                    header = f"r{len(data):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                    notified_socket.send(header + data)
                else:
                    raise RequestException(
                        msg=f"Username {request['query'].decode(ENCODING_FORMAT)} not found",
                        code=ExceptionCode.NOT_FOUND,
                    )
            elif request["type"] == "l":
                # Handle request to lookup clients by IP address
                lookup_addr = request["query"].decode(ENCODING_FORMAT)
                for key, value in clients.items():
                    if value[0] == lookup_addr:
                        username = key.encode(ENCODING_FORMAT)
                        header = f"l{len(username):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                        notified_socket.send(header + username)
                        break
                else:
                    raise RequestException(
                        msg=f"Username for {lookup_addr} not found",
                        code=ExceptionCode.NOT_FOUND,
                    )
            else:
                raise RequestException(
                    msg=f"Bad request from {notified_socket.getpeername()}",
                    code=ExceptionCode.BAD_REQUEST,
                )
        except TypeError as e:
            logging.log(level=logging.ERROR, msg=e)
            sys.exit(0)
        except RequestException as e:
            # Handle exceptions and send error responses
            if e.code == ExceptionCode.DISCONNECT:
                try:
                    active_sockets.remove(notified_socket)
                    # Remove client from clients dictionary
                except ValueError:
                    logging.info("already removed")
            else:
                data: bytes = msgpack.packb(
                    e, default=RequestException.to_dict, use_bin_type=True
                )
                header = f"e{len(data):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                notified_socket.send(header + data)
            logging.log(level=logging.ERROR, msg=f"Exception: {e.msg}")
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
        handle_client_socket(notified_socket)

