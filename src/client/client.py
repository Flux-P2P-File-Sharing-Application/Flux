import logging
import os
import re
import select
import signal
import socket
import threading
import sys
from pathlib import Path

import msgpack
import tqdm
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from ..utils.exceptions import ExceptionCode, RequestException
from ..utils.types import FileMetadata, FileRequest, FileSearchResult, HeaderCode

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7

# Server IP and port configuration
SERVER_IP = input("Enter SERVER IP: ")
SERVER_PORT = 1234
SERVER_ADDR = (SERVER_IP, SERVER_PORT)

# Encoding format
ENCODING_FORMAT = "utf-8"

# Client IP and port configuration
CLIENT_IP = socket.gethostbyname(socket.gethostname())
CLIENT_SEND_PORT = 5678
CLIENT_RECV_PORT = 4321
SHARE_FOLDER_PATH = Path("./share")
RECV_FOLDER_PATH = Path("./downloads")
FILE_BUFFER_LEN = 4096

# Ensure the necessary directories exist
log_directory = Path("./logs")
log_directory.mkdir(parents=True, exist_ok=True)
SHARE_FOLDER_PATH.mkdir(parents=True, exist_ok=True)
RECV_FOLDER_PATH.mkdir(parents=True, exist_ok=True)

# Configure logging
log_filename = log_directory / f"client_{CLIENT_IP}.log"
logging.basicConfig(
    filename=log_filename, level=logging.DEBUG
)

# Create sockets for sending and receiving messages
client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_recv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Set socket options to allow address reuse
client_send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

# Bind the sockets to the client IP and ports
client_send_socket.bind((CLIENT_IP, CLIENT_SEND_PORT))
client_recv_socket.bind((CLIENT_IP, CLIENT_RECV_PORT))

# Connect the send socket to the server
client_send_socket.connect((SERVER_IP, SERVER_PORT))

# Listen for incoming connections on the receive socket
client_recv_socket.listen(5)

# List to keep track of connected sockets
connected = [client_recv_socket]

def get_sharable_files() -> list[FileMetadata]:
    """
    Get a list of sharable files from the specified share folder path.
    Returns:
        list[FileMetadata]: A list of dictionaries containing the name and size of each sharable file.
    """
    shareable_files: list[FileMetadata] = []
    for (root, _, files) in os.walk(str(SHARE_FOLDER_PATH)):
        for f in files:
            fname = root + "/" + f
            file_size = Path(fname).stat().st_size
            shareable_files.append({"name": fname, "size": file_size})
    return shareable_files

def prompt_username() -> str:
    """
    Prompt the user for a username and send it to the server for registration.
    """
    my_username = input("Enter username: ")
    username = my_username.encode(ENCODING_FORMAT)
    username_header = f"{HeaderCode.NEW_CONNECTION.value}{len(username):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
    client_send_socket.send(username_header + username)
    type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    return type

def request_file(file_requested: FileSearchResult, client_peer_socket: socket.socket) -> str:
    file_recv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    file_recv_socket.bind((CLIENT_IP, 0))
    file_recv_socket.listen()
    file_recv_port = file_recv_socket.getsockname()[1]

    file_request = {"port": file_recv_port, "filepath": file_requested.filepath}
    file_req_bytes = msgpack.packb(file_request)
    file_req_header = (
        f"{HeaderCode.FILE_REQUEST.value}{len(file_req_bytes):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
    )
    client_peer_socket.send(file_req_header + file_req_bytes)
    
    res_type = client_peer_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    
    if res_type == HeaderCode.FILE_REQUEST.value:
        sender, _ = file_recv_socket.accept()
        logging.debug(msg=f"Sender tried to connect: {sender.getpeername()}")
        res_type = sender.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
        
        if res_type == HeaderCode.FILE.value:
            file_header_len = int(sender.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
            file_header: FileMetadata = msgpack.unpackb(sender.recv(file_header_len))
            logging.debug(msg=f"receiving file with metadata {file_header}")
            write_path: Path = get_unique_filename(RECV_FOLDER_PATH / file_header["name"])
            
            try:
                file_to_write = write_path.open("wb")
                logging.debug(f"Creating and writing to {write_path}")
                
                try:
                    byte_count = 0
                    
                    with tqdm.tqdm(
                        total=file_header["size"],
                        desc=f"Receiving {file_header['name']}",
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                    ) as progress:
                        while byte_count != file_header["size"]:
                            file_bytes_read: bytes = sender.recv(FILE_BUFFER_LEN)
                            byte_count += len(file_bytes_read)
                            file_to_write.write(file_bytes_read)
                            progress.update(len(file_bytes_read))
                        file_to_write.close()
                        return "Succesfully received 1 file"
                except Exception as e:
                    logging.error(e)
                    return "File received but failed to save"
            except Exception as e:
                logging.error(e)
                return "Unable to write file"
        else:
            raise RequestException(
                f"Sender sent invalid message type in header: {res_type}",
                ExceptionCode.INVALID_HEADER,
            )
    elif res_type == HeaderCode.ERROR.value:
        res_len = int(client_peer_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
        res = client_peer_socket.recv(res_len)
        err: RequestException = msgpack.unpackb(
            res,
            object_hook=RequestException.from_dict,
            raw=False,
        )
        raise err
    else:
        err = RequestException(
            f"Invalid message type in header: {res_type}", ExceptionCode.INVALID_HEADER
        )
        raise err

def send_handler() -> None:
    """
    Handle sending messages to other clients, including file transfers.
    """
    global client_send_socket
    with patch_stdout():
        # Prompt for the recipient's username
        recipient_prompt: PromptSession = PromptSession("\nEnter recipient's username: ")
        while True:
            recipient = recipient_prompt.prompt()
            if recipient == "!exit":
                # Exit the program
                if os.name == "nt":
                    os._exit(0)
                else:
                    os.kill(os.getpid(), signal.SIGINT)
            if recipient:
                # Encode recipient username and send a request for recipient's address
                recipient = recipient.encode(ENCODING_FORMAT)
                request_header = (
                    f"{HeaderCode.REQUEST_UNAME.value}{len(recipient):<{HEADER_MESSAGE_LENGTH}}".encode(
                        ENCODING_FORMAT
                    )
                )
                logging.debug(msg=f"Sent packet {(request_header + recipient).decode(ENCODING_FORMAT)}")                
                client_send_socket.send(request_header + recipient)
                
                # Receive and decode the response type and length
                res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                logging.debug(msg=f"Response type: {res_type}")
                response_length = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
                response = client_send_socket.recv(response_length)
                
                if res_type == HeaderCode.REQUEST_UNAME.value:
                    # Get recipient address and establish a connection
                    recipient_addr: str = response.decode(ENCODING_FORMAT)
                    logging.debug(msg=f"Response: {recipient_addr}")
                    client_peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    client_peer_socket.connect((recipient_addr, CLIENT_RECV_PORT))
                    
                    while True:
                        # Prompt for a message to send to the recipient
                        msg_prompt: PromptSession = PromptSession(f"\nEnter message for {recipient.decode(ENCODING_FORMAT)}: ")
                        msg = msg_prompt.prompt()
                        if len(msg):
                            msg.strip().split()
                            send_res_list = [
                                r"!send (\S+)$",
                                r"!send '(.+)'$",
                                r'!send "(.+)"$',
                            ]
                            search_res_list = [
                                r"!search (\S+)$",
                                r"!search '(.+)'$",
                                r'!search "(.+)"$',
                            ]
                            filename = ""
                            for r in send_res_list:
                                match_res = re.match(r, msg)
                                if match_res:
                                    filename = match_res.group(1)
                                    break
                            searchquery = ""
                            for r in search_res_list:
                                match_res = re.match(r, msg)
                                if match_res:
                                    searchquery = match_res.group(1)
                                    break
                            if filename:
                                filepath: Path = SHARE_FOLDER_PATH / filename
                                logging.debug(f"{filepath} chosen to send")
                                if filepath.exists() and filepath.is_file():
                                    filemetadata: FileMetadata = {
                                        "name": filename.split("/")[-1],
                                        "size": filepath.stat().st_size,
                                    }
                                    logging.debug(filemetadata)
                                    filemetadata_bytes = msgpack.packb(filemetadata)
                                    logging.debug(filemetadata_bytes)
                                    filesend_header = f"{HeaderCode.FILE.value}{len(filemetadata_bytes):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                                    try:
                                        file_to_send = filepath.open(mode="rb")
                                        logging.debug(f"Sending file {filename} to {recipient.decode(ENCODING_FORMAT)}")
                                        
                                        # Send file metadata header and file content
                                        client_peer_socket.send(filesend_header + filemetadata_bytes)
                                        with tqdm.tqdm(
                                            total=filemetadata["size"],
                                            desc=f"Sending {str(filepath)}",
                                            unit="B",
                                            unit_scale=True,
                                            unit_divisor=1024,
                                        ) as progress:
                                            total_bytes_read = 0
                                            while total_bytes_read != filemetadata["size"]:
                                                bytes_read = file_to_send.read(FILE_BUFFER_LEN)
                                                client_peer_socket.sendall(bytes_read)
                                                num_bytes = len(bytes_read)
                                                total_bytes_read += num_bytes
                                                progress.update(num_bytes)
                                            progress.close()
                                            print("File Sent")
                                            file_to_send.close()
                                    except Exception as e:
                                        logging.error(f"File Sending failed: {e}")
                                else:
                                    logging.error(f"{filepath} not found")
                                    print(f"Unable to perform send request, ensure that the file is available in {SHARE_FOLDER_PATH}")
                            elif searchquery:
                                searchquery_bytes = searchquery.encode(ENCODING_FORMAT)
                                search_header = f"{HeaderCode.FILE_SEARCH.value}{len(searchquery_bytes):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                                client_send_socket.send(
                                    search_header + searchquery_bytes
                                )
                                response_header_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                                if (
                                    response_header_type
                                    == HeaderCode.FILE_SEARCH.value
                                ):
                                    response_len = int(
                                        client_send_socket.recv(HEADER_MESSAGE_LENGTH)
                                        .decode(ENCODING_FORMAT)
                                        .strip()
                                    )
                                    search_result_list: list[list[str | int]] = msgpack.unpackb(
                                        client_send_socket.recv(response_len),
                                        use_list=False,
                                    )
                                    search_result = [
                                        FileSearchResult(*result) for result in search_result_list
                                    ]
                                    for i, res in enumerate(search_result):
                                        print(f"{i+1} PATH: {res.filepath} \n\t USER: {res.uname}")
                                    file_choice_prompt: PromptSession = PromptSession(
                                        "Enter choice: "
                                    )
                                    choice: str = file_choice_prompt.prompt()

                                    if not choice.isdigit() or not (
                                        1 <= int(choice) <= len(search_result)
                                    ):
                                        continue

                                    file_requested = search_result[int(choice) - 1]
                                    uname_bytes = file_requested.uname.encode(ENCODING_FORMAT)
                                    request_header = f"{HeaderCode.REQUEST_UNAME.value}{len(uname_bytes):<{HEADER_MESSAGE_LENGTH}}".encode(
                                        ENCODING_FORMAT
                                    )
                                    logging.debug(
                                        msg=f"Sent packet {(request_header + uname_bytes).decode(ENCODING_FORMAT)}"
                                    )
                                    client_send_socket.send(request_header + uname_bytes)
                                    res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                                    logging.debug(msg=f"Response type: {res_type}")
                                    response_length = int(
                                        client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip()
                                    )
                                    response = client_send_socket.recv(response_length)
                                    if res_type == HeaderCode.REQUEST_UNAME.value:
                                        req_file_thread = threading.Thread(
                                            target=request_file,
                                            args=(
                                                file_requested,
                                                client_peer_socket,
                                            ),
                                        )
                                        req_file_thread.start()
                                    elif res_type == HeaderCode.ERROR.value:
                                        res_len = int(
                                            client_send_socket.recv(HEADER_MESSAGE_LENGTH)
                                            .decode(ENCODING_FORMAT)
                                            .strip()
                                        )
                                        res = client_send_socket.recv(res_len)
                                        error: RequestException = msgpack.unpackb(
                                            res,
                                            object_hook=RequestException.from_dict,
                                            raw=False,
                                        )
                                        logging.error(msg=error)
                                    else:
                                        logging.error(f"Invalid message type in header: {res_type}")
                                else:
                                    logging.error("Error occured while searching for files")
                            else:
                                # Send a text message
                                msg = msg.encode(ENCODING_FORMAT)
                                if msg == b"!exit":
                                    break
                                header = f"{HeaderCode.MESSAGE.value}{len(msg):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                                client_peer_socket.send(header + msg)
                elif res_type == HeaderCode.ERROR.value:
                    # Handle errors
                    err: RequestException = msgpack.unpackb(
                        response, 
                        object_hook=RequestException.from_dict, 
                        raw=False
                    )
                    logging.error(msg=err)
                else:
                    logging.error(f"Invalid message type in header: {res_type}")

def send_file(filepath: Path, requester: tuple[str, int]):
    file_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    file_send_socket.connect(requester)

    filemetadata: FileMetadata = {
        "name": str(filepath).split("/")[-1],
        "size": filepath.stat().st_size,
    }
    logging.debug(filemetadata)
    filemetadata_bytes = msgpack.packb(filemetadata)
    logging.debug(filemetadata_bytes)
    filesend_header = f"{HeaderCode.FILE.value}{len(filemetadata_bytes):<{HEADER_MESSAGE_LENGTH}}".encode(
        ENCODING_FORMAT
    )

    try:
        file_to_send = filepath.open(mode="rb")
        logging.debug(f"Sending file {filemetadata['name']} to {requester}")
        file_send_socket.send(filesend_header + filemetadata_bytes)
        with tqdm.tqdm(
            total=filemetadata["size"],
            desc=f"Sending {str(filepath)}",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as progress:
            total_bytes_read = 0
            while total_bytes_read != filemetadata["size"]:
                bytes_read = file_to_send.read(FILE_BUFFER_LEN)
                file_send_socket.sendall(bytes_read)
                num_bytes = len(bytes_read)
                total_bytes_read += num_bytes
                progress.update(num_bytes)
            progress.close()
            print("File Sent")
            file_to_send.close()
            file_send_socket.close()
    except Exception as e:
        logging.error(f"File Sending failed: {e}")


def get_unique_filename(path: Path) -> Path:
    """
    Generate a unique filename in the specified path to avoid overwriting.
    """
    filename, extension = path.stem, path.suffix
    counter = 1

    while path.exists():
        path = RECV_FOLDER_PATH / Path(filename + "_" + str(counter) + extension)
        counter += 1

    logging.debug(f"Unique file name is {path}")
    return path

def receive_msg(socket: socket.socket) -> str:
    """
    Receive a message or file from the given socket.
    """
    logging.debug(f"Receiving from {socket.getpeername()}")
    
    # Receive and decode the message type
    message_type = socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT)
    if not len(message_type):
        # Raise an exception if the peer has closed the connection
        raise RequestException(
            msg=f"Peer at {socket.getpeername()} closed the connection",
            code=ExceptionCode.DISCONNECT,
        )
    elif message_type not in [
        HeaderCode.MESSAGE.value,
        HeaderCode.FILE.value,
        HeaderCode.FILE_REQUEST.value,
    ]:
        # Raise an exception for invalid message type
        raise RequestException(
            msg=f"Invalid message type in header. Received [{message_type}]",
            code=ExceptionCode.INVALID_HEADER,
        )
    else:
        if message_type == HeaderCode.FILE.value:
            # Receive file header length and file metadata
            file_header_len = int(socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
            file_header: FileMetadata = msgpack.unpackb(socket.recv(file_header_len))
            logging.debug(msg=f"Receiving file with metadata {file_header}")
            
            # Get unique filename and prepare to write the file
            write_path: Path = get_unique_filename(RECV_FOLDER_PATH / file_header["name"])
            try:
                file_to_write = open(str(write_path), "wb")
                logging.debug(f"Creating and writing to {write_path}")
                try:
                    byte_count = 0
                    # Display a progress bar for file reception
                    with tqdm.tqdm(
                        total=file_header["size"],
                        desc=f"Receiving {file_header['name']}",
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                    ) as progress:
                        while byte_count != file_header["size"]:
                            # Receive file bytes and write to the file
                            file_bytes_read: bytes = socket.recv(FILE_BUFFER_LEN)
                            byte_count += len(file_bytes_read)
                            file_to_write.write(file_bytes_read)
                            progress.update(len(file_bytes_read))
                        progress.close()
                        file_to_write.close()
                        return "Successfully received 1 file"
                except Exception as e:
                    logging.error(e)
                    return "File received but failed to save"
            except Exception as e:
                logging.error(e)
                return "Unable to write file"
        elif message_type == HeaderCode.FILE_REQUEST.value:
            req_header_len = int(socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
            file_req_header: FileRequest = msgpack.unpackb(socket.recv(req_header_len))
            logging.debug(msg=f"Received request: {file_req_header}")
            requested_file_path = Path(file_req_header["filepath"])
            if requested_file_path.is_file():
                socket.send(HeaderCode.FILE_REQUEST.value.encode(ENCODING_FORMAT))
                send_file_thread = threading.Thread(
                    target=send_file,
                    args=(requested_file_path, (socket.getpeername()[0], file_req_header["port"])),
                )
                send_file_thread.start()
                return "File requested by user"
            else:
                raise RequestException(
                    f"Requested file {file_req_header['filepath']} is not available",
                    ExceptionCode.NOT_FOUND,
                )
        else:
            # Receive and decode a regular text message
            message_len = int(socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
            return socket.recv(message_len).decode(ENCODING_FORMAT)

def receive_handler() -> None:
    """
    Handle receiving messages or files from other clients.
    """
    global client_send_socket
    global client_recv_socket
    peers: dict[str, str] = {}  # Dictionary to store peer addresses and usernames
    
    while True:
        # Use select to wait for socket activity
        read_sockets, _, __ = select.select(connected, [], [], 1)
        
        for notified_socket in read_sockets:
            if notified_socket == client_recv_socket:
                # Accept new connections
                peer_socket, peer_addr = client_recv_socket.accept()
                logging.debug(
                    msg=(
                        "Accepted new connection from"
                        f" {peer_addr[0]}:{peer_addr[1]}"
                    )
                )
                try:
                    connected.append(peer_socket)
                    lookup = peer_addr[0].encode(ENCODING_FORMAT)
                    header = (
                        f"{HeaderCode.LOOKUP_ADDRESS.value}{len(lookup):<{HEADER_MESSAGE_LENGTH}}".encode(
                            ENCODING_FORMAT
                        )
                    )
                    logging.debug(msg=f"Sending packet {(header + lookup).decode(ENCODING_FORMAT)}")
                    client_send_socket.send(header + lookup)
                    
                    res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(
                        ENCODING_FORMAT
                    )
                    if res_type not in [
                        HeaderCode.LOOKUP_ADDRESS.value,
                        HeaderCode.ERROR.value,
                    ]:
                        raise RequestException(
                            msg="Invalid message type in header",
                            code=ExceptionCode.INVALID_HEADER,
                        )
                    else:
                        response_length = int(
                            client_send_socket.recv(HEADER_MESSAGE_LENGTH)
                            .decode(ENCODING_FORMAT)
                            .strip()
                        )
                        response = client_send_socket.recv(response_length)
                        if res_type == HeaderCode.LOOKUP_ADDRESS.value:
                            username = response.decode(ENCODING_FORMAT)
                            print(f"User {username} is trying to send a message")
                            peers[peer_addr[0]] = username
                        else:
                            exception = msgpack.unpackb(
                                response,
                                object_hook=RequestException.from_dict,
                                raw=False,
                            )
                            logging.error(msg=exception)
                            raise exception
                except RequestException as e:
                    logging.error(msg=e)
                    break
            else:
                try:
                    # Receive message from the notified socket
                    msg: str = receive_msg(notified_socket)
                    username = peers[notified_socket.getpeername()[0]]
                    print(f"{username} > {msg}")
                except RequestException as e:
                    if e.code == ExceptionCode.DISCONNECT:
                        try:
                            connected.remove(notified_socket)
                        except ValueError:
                            logging.info("already removed")
                    logging.error(msg = f"Exception: {e.msg}")
                    break

def excepthook(args: threading.ExceptHookArgs) -> None:
    logging.fatal(msg=args)
    logging.fatal(msg=args.exc_traceback)

if __name__ == "__main__":
    try:
        while prompt_username() != HeaderCode.NEW_CONNECTION.value:
            error_len = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
            error = client_send_socket.recv(error_len)
            exception: RequestException = msgpack.unpackb(
                error, object_hook=RequestException.from_dict, raw=False
            )
            if exception.code == ExceptionCode.USER_EXISTS:
                logging.error(msg=exception.msg)
                print("Sorry that username is taken, please choose another one")
            else:
                logging.fatal(msg=exception.msg)
                print("Sorry something went wrong")
                client_send_socket.close()
                client_recv_socket.close()
                sys.exit(1)
        print("Successfully registered")
        share_data = msgpack.packb(get_sharable_files())
        share_data_header = (
            f"{HeaderCode.SHARE_DATA.value}{len(share_data):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
        )
        client_send_socket.send(share_data_header + share_data)
        threading.excepthook = excepthook
        send_thread = threading.Thread(target=send_handler)
        receive_thread = threading.Thread(target=receive_handler)
        send_thread.start()
        receive_thread.start()
    except (KeyboardInterrupt, EOFError, SystemExit):
        sys.exit(0)
    except:
        logging.fatal(msg=sys.exc_info()[0])
        sys.exit(1)
