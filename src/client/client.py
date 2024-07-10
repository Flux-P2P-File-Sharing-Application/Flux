import logging
import os
import re
import select
import signal
import socket
import threading
from pathlib import Path

import msgpack
import tqdm
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from ..exceptions import ExceptionCode, RequestException
from ..headers import FileMetadata, HeaderCode

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
SHARE_FOLDER_PATH = Path("share")
RECV_FOLDER_PATH = Path("downloads")
FILE_BUFFER_LEN = 4096

# Ensure the logs directory exists
log_directory = "./logs"
os.makedirs(log_directory, exist_ok=True)

# Configure logging
log_filename = f"{log_directory}/client_{CLIENT_IP}.log"
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
                request_header = f"{HeaderCode.REQUEST_UNAME.value}{len(recipient):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
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
                            res = [
                                r"!send (\S+)$",
                                r"!send '(.+)'$",
                                r'!send "(.+)"$',
                            ]
                            filename = ""
                            for r in res:
                                match_res = re.match(r, msg)
                                if match_res:
                                    filename = match_res.group(1)
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
                            else:
                                # Send a text message
                                msg = msg.encode(ENCODING_FORMAT)
                                if msg == b"!exit":
                                    break
                                header = f"{HeaderCode.MESSAGE.value}{len(msg):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                                client_peer_socket.send(header + msg)
                if res_type == HeaderCode.ERROR.value:
                    # Handle errors
                    err: RequestException = msgpack.unpackb(response, object_hook=RequestException.from_dict, raw=False)
                    logging.error(msg=err)


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
    elif message_type not in [HeaderCode.MESSAGE.value, HeaderCode.FILE.value]:
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
                    lookup: bytes = peer_addr[0].encode(ENCODING_FORMAT)
                    header = f"{HeaderCode.LOOKUP_ADDRESS.value}{len(lookup):<{HEADER_MESSAGE_LENGTH}}".encode(
                        ENCODING_FORMAT
                    )
                    logging.debug(
                        msg=f"Sending packet {(header + lookup).decode(ENCODING_FORMAT)}"
                    )
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

def main():
    """
    Main function to start the client and initiate communication.
    """
    try:
        if prompt_username() == HeaderCode.NEW_CONNECTION.value:
            print("Connection to the server was successful")
            recv_thread = threading.Thread(target=receive_handler, daemon=True)
            recv_thread.start()
            send_handler()
        else:
            raise RequestException(msg="Unable to connect to the server", code=ExceptionCode.DISCONNECT)
    except KeyboardInterrupt:
        if os.name == "nt":
            os._exit(0)
        else:
            os.kill(os.getpid(), signal.SIGINT)
    except Exception as e:
        logging.error(e)
        client_send_socket.close()
        client_recv_socket.close()

if __name__ == "__main__":
    main()
