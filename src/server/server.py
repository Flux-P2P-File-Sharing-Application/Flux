import logging
import select
import socket
import sqlite3
import sys
import os

import msgpack

from ..utils.exceptions import ExceptionCode, RequestException
from ..utils.types import FileMetadata, FileSearchResult, HeaderCode, Message, UpdateHashParams

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
SERVER_IP = socket.gethostbyname(socket.gethostname())
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
sockets_list  = [server_socket]

# Mapping of usernames to addresses (IP, PORT)
uname_to_ip: dict[str, str] = {}
# Mapping of IP to usernames
ip_to_uname: dict[str, str] = {}

# Connect to the SQLite database (creates the file if it doesn't exist)
db_connection = sqlite3.connect("files.db")
db = db_connection.cursor()

# Create table for files if it doesn't exist
db.execute(
    """
    CREATE TABLE IF NOT EXISTS files (
        uname TEXT NOT NULL,
        filepath TEXT NOT NULL,
        filesize INTEGER NOT NULL,
        hash TEXT,
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


# NEW SHARE_DATA
def insert_share_data(uname: str, filepath: str, filesize: int, hash: str | None):
    query = """
            INSERT INTO files(uname, filepath, filesize, hash) VALUES (?, ?, ?, ?)
            """
    args = (uname, filepath, filesize, hash)
    db.execute(query, args)


def search_files(query_string: str, self_uname: str) -> list[FileSearchResult]:
    query = """
            SELECT uname, filepath, filesize, hash FROM files WHERE filepath LIKE ? AND uname != ?
            """
    args = ("%" + query_string + "%", self_uname)
    db.execute(query, args)
    return [FileSearchResult(*result) for result in db.fetchall()]


def update_file_hash(uname: str, filepath: str, hash: str) -> None:
    query = """
            UPDATE files SET hash = ? WHERE uname = ? AND filepath = ?
            """
    args = (hash, uname, filepath)
    db.execute(query, args)

def receive_msg(client_socket: socket.socket) -> Message:
    """
    Receive a message from the client socket.
    """
    message_type = client_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
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
        message_len = int(client_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
        query = client_socket.recv(message_len)
        logging.debug(
            msg=f"Received packet: TYPE {message_type} QUERY {query!r} from {client_socket.getpeername()}"
        )
        return {"type": HeaderCode(message_type), "query": query}

def read_handler(notified_socket: socket.socket) -> None:
    """
    Handle incoming requests from clients.
    """
    global uname_to_ip
    global sockets_list 
    logging.info(f"CLIENTS {uname_to_ip}")
    
    # Accept new connections
    if notified_socket == server_socket:
        client_socket, client_address = server_socket.accept()
        try:
            # Process initial message from new client
            user_data = receive_msg(client_socket)
            username = user_data["query"].decode(ENCODING_FORMAT)
            sockets_list .append(client_socket)

            if user_data["type"] == HeaderCode.NEW_CONNECTION:
                address = uname_to_ip.get(username)
                logging.debug(
                    msg=f"Registration request for username {username} from address {client_address}"
                )
                # Check if the username is already registered
                if address is None:
                    uname_to_ip[username] = client_address[0]
                    ip_to_uname[client_address[0]] = username
                    logging.debug(
                        msg=(
                            "Accepted new connection from"
                            f" {client_address[0]}:{client_address[1]}"
                            f" username: {username}"
                        ),
                    )
                    client_socket.send(f"{HeaderCode.NEW_CONNECTION.value}".encode(ENCODING_FORMAT))
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
                header = f"{HeaderCode.ERROR.value}{len(data):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                client_socket.send(header + data)

            uname = ip_to_uname.pop(client_address[0], None)
            uname_to_ip.pop(uname, None)

            logging.error(msg=e.msg)
            return
    else:
        try:
            # Handle requests from existing clients
            request = receive_msg(notified_socket)
            match request["type"]:
                case HeaderCode.REQUEST_UNAME:
                    response_data = uname_to_ip.get(request["query"].decode(ENCODING_FORMAT))
                    if response_data is not None:
                        if response_data != notified_socket.getpeername()[0]:
                            logging.debug(msg=f"Valid request: {response_data}")
                            data = response_data.encode(ENCODING_FORMAT)
                            header = f"{HeaderCode.REQUEST_UNAME.value}{len(data):<{HEADER_MESSAGE_LENGTH}}".encode(
                                ENCODING_FORMAT
                            )
                            notified_socket.send(header + data)
                        else:
                            raise RequestException(
                                msg="Cannot query for user having the same address",
                                code=ExceptionCode.BAD_REQUEST,
                            )
                    else:
                        raise RequestException(
                            msg=f"Username {request['query'].decode(ENCODING_FORMAT)} not found",
                            code=ExceptionCode.NOT_FOUND,
                        )
                case HeaderCode.LOOKUP_ADDRESS:
                    lookup_addr = request["query"].decode(ENCODING_FORMAT)
                    if lookup_addr != notified_socket.getpeername()[0]:
                        username = ip_to_uname.get(lookup_addr)
                        if username is not None:
                            username_bytes = username.encode(ENCODING_FORMAT)
                            header = f"{HeaderCode.LOOKUP_ADDRESS.value}{len(username):<{HEADER_MESSAGE_LENGTH}}".encode(
                                ENCODING_FORMAT
                            )
                            notified_socket.send(header + username_bytes)
                        else:
                            raise RequestException(
                                msg=f"Username for {lookup_addr} not found",
                                code=ExceptionCode.NOT_FOUND,
                            )
                    else:
                        raise RequestException(
                            msg="Cannot query for user having the same address",
                            code=ExceptionCode.BAD_REQUEST,
                        )
                        
                case HeaderCode.NEW_CONNECTION:
                    uname = request["query"].decode(ENCODING_FORMAT)
                    addr = uname_to_ip.get(uname)
                    client_addr = notified_socket.getpeername()
                    logging.debug(
                        f"Registration request for username {uname} from address {client_addr}"
                    )
                    if addr is None:
                        uname_to_ip[uname] = client_addr[0]
                        logging.debug(
                            msg=(
                                "Accepted new connection from"
                                f" {client_addr[0]}:{client_addr[1]}"
                                f" username: {uname}"
                            )
                        )
                        notified_socket.send(f"{HeaderCode.NEW_CONNECTION.value}".encode(ENCODING_FORMAT))
                    else:
                        if addr != client_addr[0]:
                            raise RequestException(
                                msg=f"User with username {addr} already exists",
                                code=ExceptionCode.USER_EXISTS,
                            )
                        else:
                            raise RequestException(
                                msg="Cannot re-register user for same address",
                                code=ExceptionCode.BAD_REQUEST,
                            )
                case HeaderCode.SHARE_DATA:
                    share_data: list[FileMetadata] = msgpack.unpackb(request["query"])
                    username = ip_to_uname.get(notified_socket.getpeername()[0])
                    if username is not None:
                        for file_data in share_data:
                            insert_share_data(
                                username,
                                file_data["name"],
                                file_data["size"],
                                file_data.get("hash", None),
                            )
                        db_connection.commit()
                    else:
                        raise RequestException(
                            msg=f"Username does not exist",
                            code=ExceptionCode.NOT_FOUND,
                        )
                case HeaderCode.UPDATE_HASH:
                    update_hash_params: UpdateHashParams = msgpack.unpackb(request["query"])
                    username = ip_to_uname.get(notified_socket.getpeername()[0])
                    if username is not None:
                        update_file_hash(
                            username,
                            update_hash_params["filepath"],
                            update_hash_params["hash"],
                        )
                    else:
                        raise RequestException(
                            msg=f"Username does not exist",
                            code=ExceptionCode.NOT_FOUND,
                        )
                case HeaderCode.FILE_SEARCH:
                    username = ip_to_uname.get(notified_socket.getpeername()[0])
                    if username is not None:
                        search_result = search_files(request["query"].decode(ENCODING_FORMAT), username)
                        logging.debug(f"{search_result}")
                        search_result_bytes = msgpack.packb(search_result)
                        search_result_header = f"{HeaderCode.FILE_SEARCH.value}{len(search_result_bytes):<{HEADER_MESSAGE_LENGTH}}".encode(
                            ENCODING_FORMAT
                        )
                        notified_socket.send(search_result_header + search_result_bytes)
                    else:
                        raise RequestException(
                            msg=f"Username does not exist",
                            code=ExceptionCode.NOT_FOUND,
                        )
                case _:
                    raise RequestException(
                        msg=f"Bad request from {notified_socket.getpeername()}",
                        code=ExceptionCode.BAD_REQUEST,
                    )
        except RequestException as e:
            # Handle exceptions and send error responses
            if e.code != ExceptionCode.DISCONNECT:
                data = msgpack.packb(
                    e, default=RequestException.to_dict, use_bin_type=True
                )
                header = f"{HeaderCode.ERROR.value}{len(data):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                notified_socket.send(header + data)
            client_address = notified_socket.getpeername()
            uname = ip_to_uname.pop(client_address[0], None)
            uname_to_ip.pop(uname, None)
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
                sockets_list, [], sockets_list
            )
            for notified_socket in read_sockets:
                read_handler(notified_socket)
            for notified_socket in exception_sockets:
                # Close the socket if there's an exception
                sockets_list.remove(notified_socket)
                notified_socket.close()
    except KeyboardInterrupt:
        logging.info("Server shutting down.")
    finally:
        # Clean up resources on server shutdown
        server_socket.close()
        db_connection.close()

if __name__ == "__main__":
    main()
