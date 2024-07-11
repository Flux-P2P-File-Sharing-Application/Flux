import logging
import select
import socket
import sqlite3
import sys
import os

import msgpack

from ..exceptions import ExceptionCode, RequestException
from ..headers import FileMetadata, FileSearchResult, HeaderCode, Message

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
# Mapping of IP to usernames
ip_to_uname: dict[str, str] = {}

# Connect to the SQLite database (creates the file if it doesn't exist)
db_connection = sqlite3.connect("files.db")
db = db_connection.cursor()

# Create table for files if it doesn't exist
db.execute(
    """
    CREATE TABLE IF NOT EXISTS files (
        uname TEXT,
        filepath TEXT,
        filesize INTEGER,
        PRIMARY KEY (uname, filepath)
    )
    """
)
db.execute(
    """
    CREATE INDEX IF NOT EXISTS files_by_filepath ON files(filepath)
    """
)
db_connection.commit()

def insert_share_data(uname: str, filepath: str, filesize: int):
    """
    Insert shared data into the database.
    """
    query = """
            INSERT INTO files(uname, filepath, filesize) VALUES (?, ?, ?)
            """
    args = (uname, filepath, filesize)
    db.execute(query, args)
    db_connection.commit()

def search_files(query_string: str, self_uname: str) -> list[FileSearchResult]:
    """
    Search for files in the database.
    """
    query = """
            SELECT uname, filepath, filesize FROM files WHERE filepath LIKE ? AND uname != ?
            """
    args = ("%" + query_string + "%", self_uname)
    db.execute(query, args)
    return db.fetchall()

def receive_msg(client_socket: socket.socket) -> Message:
    """
    Receive a message from the client socket.
    """
    message_type = client_socket.recv(HEADER_TYPE_LEN).decode(FMT)
    if not len(message_type):
        # Handle client disconnect
        raise RequestException(
            msg=f"Client at {client_socket.getpeername()} closed the connection",
            code=ExceptionCode.DISCONNECT,
        )
    if message_type not in [
        HeaderCode.NEW_CONNECTION.value,
        HeaderCode.REQUEST_UNAME.value,
        HeaderCode.LOOKUP_ADDRESS.value,
        HeaderCode.SHARE_DATA.value,
        HeaderCode.FILE_SEARCH.value,
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
            msg=f"Received packet: TYPE {message_type} QUERY {query!r} from {client_socket.getpeername()}"
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
                    ip_to_uname[client_address[0]] = username
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
                data = msgpack.packb(
                    e, default=RequestException.to_dict, use_bin_type=True
                )
                header = f"{HeaderCode.ERROR.value}{len(data):<{HEADER_MSG_LEN}}".encode(FMT)
                client_socket.send(header + data)

            uname = ip_to_uname.pop(client_address[0], None)
            clients.pop(uname, None)

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
                    if response_data != notified_socket.getpeername()[0]:
                        logging.debug(msg=f"Valid request: {response_data}")
                        data = response_data.encode(FMT)
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
                    username = ip_to_uname.get(lookup_address)
                    if username is not None:
                        username_bytes = username.encode(FMT)
                        header = f"{HeaderCode.LOOKUP_ADDRESS.value}{len(username):<{HEADER_MSG_LEN}}".encode(FMT)
                        notified_socket.send(header + username_bytes)
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
            elif request["type"] == HeaderCode.NEW_CONNECTION:
                uname = request["query"].decode(FMT)
                address = clients.get(uname)
                client_address = notified_socket.getpeername()
                logging.debug(
                    f"Registration request for username {uname} from address {client_address}"
                )
                if address is None:
                    clients[uname] = client_address[0]
                    ip_to_uname[client_address[0]] = uname
                    logging.debug(
                        msg=(
                            "Accepted new connection from"
                            f" {client_address[0]}:{client_address[1]}"
                            f" username: {uname}"
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
            elif request["type"] == HeaderCode.SHARE_DATA:
                # Handle share data request
                share_data: list[FileMetadata] = msgpack.unpackb(request["query"])
                username = ip_to_uname.get(notified_socket.getpeername()[0])
                if username is not None:
                    for file_data in share_data:
                        insert_share_data(
                            username, file_data["name"], file_data["size"]
                        )
                else:
                    raise RequestException(
                        msg=f"Username does not exist for address {notified_socket.getpeername()}",
                        code=ExceptionCode.NOT_FOUND,
                    )
            elif request["type"] == HeaderCode.FILE_SEARCH:
                # Handle file search request
                query_str = request["query"].decode(FMT)
                username = ip_to_uname.get(notified_socket.getpeername()[0])
                if username is not None:
                    results = search_files(query_str, username)
                    logging.debug(msg=f"Results from DB {results}")
                    # Map result from tuples to FileSearchResult object
                    data = [
                        FileSearchResult(
                            uname=row[0], filepath=row[1], filesize=row[2]
                        ).to_dict()
                        for row in results
                    ]
                    data_bytes = msgpack.packb(data, use_bin_type=True)
                    header = f"{HeaderCode.FILE_SEARCH.value}{len(data_bytes):<{HEADER_MSG_LEN}}".encode(FMT)
                    notified_socket.send(header + data_bytes)
                else:
                    raise RequestException(
                        msg=f"Username does not exist for address {notified_socket.getpeername()}",
                        code=ExceptionCode.NOT_FOUND,
                    )
        except RequestException as e:
            # Handle exceptions and send error responses
            if e.code != ExceptionCode.DISCONNECT:
                data = msgpack.packb(
                    e, default=RequestException.to_dict, use_bin_type=True
                )
                header = f"{HeaderCode.ERROR.value}{len(data):<{HEADER_MSG_LEN}}".encode(FMT)
                notified_socket.send(header + data)
            client_address = notified_socket.getpeername()
            uname = ip_to_uname.pop(client_address[0], None)
            clients.pop(uname, None)
            logging.error(msg=e.msg)
            return

def main():
    """
    Main function to run the server.
    """
    logging.info(f"Server listening on {SERVER_IP}:{SERVER_PORT}")
    try:
        while True:
            read_sockets, _, exception_sockets = select.select(
                active_sockets, [], active_sockets
            )
            for notified_socket in read_sockets:
                read_handler(notified_socket)
            for notified_socket in exception_sockets:
                # Close the socket if there's an exception
                active_sockets.remove(notified_socket)
                notified_socket.close()
    except KeyboardInterrupt:
        logging.info("Server shutting down.")
    finally:
        # Clean up resources on server shutdown
        server_socket.close()
        db_connection.close()

if __name__ == "__main__":
    main()
