import logging
import os
import select
import signal
import socket
import sys
import threading

import msgpack

from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import PromptSession

from ..exceptions import ExceptionCode, RequestException
from ..headers import HeaderCode

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
CLIENT_RECV_PORT = 4328

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
    Handle sending messages to other clients.
    """
    global client_send_socket
    with patch_stdout():
        recipient_prompt: PromptSession = PromptSession("\nEnter recipient's username: ")
        while True:
            recipient = recipient_prompt.prompt()
            if recipient == "!exit":
                if os.name == "nt":
                    os._exit(0)
                else:
                    os.kill(os.getpid(), signal.SIGINT)
            if recipient:
                recipient = recipient.encode(ENCODING_FORMAT)
                request_header = f"{HeaderCode.REQUEST_UNAME.value}{len(recipient):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                logging.debug(msg=f"Sent packet {(request_header + recipient).decode(ENCODING_FORMAT)}")
                client_send_socket.send(request_header + recipient)
                res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                logging.debug(msg=f"Response type: {res_type}")
                response_length = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
                response = client_send_socket.recv(response_length)
                if res_type == HeaderCode.REQUEST_UNAME.value:
                    recipient_addr: str = response.decode(ENCODING_FORMAT)
                    logging.debug(msg=f"Response: {recipient_addr}")
                    client_peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    client_peer_socket.connect((recipient_addr, CLIENT_RECV_PORT))
                    while True:
                        msg_prompt: PromptSession = PromptSession(f"\nEnter message for {recipient.decode(ENCODING_FORMAT)}: ")
                        msg = msg_prompt.prompt()
                        if msg:
                            msg = msg.encode(ENCODING_FORMAT)
                            if msg == b"!exit":
                                break
                            header = f"{HeaderCode.MESSAGE.value}{len(msg):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                            client_peer_socket.send(header + msg)
                if res_type == HeaderCode.ERROR.value:
                    err: RequestException = msgpack.unpackb(response, object_hook=RequestException.from_dict, raw=False)
                    logging.error(msg=err)

def receive_msg(socket: socket.socket) -> str:
    """
    Receive a message from the given socket.
    """
    message_type = socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    if not message_type:
        raise RequestException(msg=f"Peer at {socket.getpeername()} closed the connection", code=ExceptionCode.DISCONNECT)
    elif message_type != HeaderCode.MESSAGE.value:
        raise RequestException(msg="Invalid message type in header", code=ExceptionCode.INVALID_HEADER)
    else:
        message_len = int(socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
        return socket.recv(message_len).decode(ENCODING_FORMAT)

def receive_handler() -> None:
    """
    Handle receiving messages from other clients.
    """
    global client_send_socket
    global client_recv_socket
    peers: dict[str, str] = {}

    while True:
        read_sockets, _, __ = select.select(connected, [], [], 1)
        for notified_socket in read_sockets:
            if notified_socket == client_recv_socket:
                peer_socket, peer_addr = client_recv_socket.accept()
                logging.debug(msg=f"Accepted new connection from {peer_addr[0]}:{peer_addr[1]}")
                try:
                    connected.append(peer_socket)
                    lookup: bytes = peer_addr[0].encode(ENCODING_FORMAT)
                    header = f"{HeaderCode.LOOKUP_ADDRESS.value}{len(lookup):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                    logging.debug(msg=f"Sending packet {(header + lookup).decode(ENCODING_FORMAT)}")
                    client_send_socket.send(header + lookup)
                    res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                    if res_type not in [HeaderCode.LOOKUP_ADDRESS.value, HeaderCode.ERROR.value]:
                        raise RequestException(msg="Invalid message type in header", code=ExceptionCode.INVALID_HEADER)
                    else:
                        response_length = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
                        response = client_send_socket.recv(response_length)
                        if res_type == HeaderCode.LOOKUP_ADDRESS.value:
                            username = response.decode(ENCODING_FORMAT)
                            print(f"User {username} is trying to send a message")
                            peers[peer_addr[0]] = username
                        else:
                            exception = msgpack.unpackb(response, object_hook=RequestException.from_dict, raw=False)
                            logging.error(msg=exception)
                            raise exception
                except RequestException as e:
                    logging.error(msg=e)
                    break
            else:
                try:
                    msg: str = receive_msg(notified_socket)
                    user_name = peers[notified_socket.getpeername()[0]]
                    print(f"{user_name} says: {msg}")
                except RequestException as e:
                    if e.code == ExceptionCode.DISCONNECT:
                        try:
                            connected.remove(notified_socket)
                        except ValueError:
                            logging.info("already removed")
                    logging.error(msg=f"Exception: {e.msg}")
                    break

def excepthook(args: threading.ExceptHookArgs):
    """
    Custom exception hook for logging fatal errors in threads.
    """
    logging.fatal(msg=args)

if __name__ == "__main__":
    # Set the custom exception hook for threading
    threading.excepthook = excepthook
    try:
        # Prompt for username until successfully registered
        while prompt_username() != HeaderCode.NEW_CONNECTION.value:
            error_len = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
            error = client_send_socket.recv(error_len)
            exception: RequestException = msgpack.unpackb(error, object_hook=RequestException.from_dict, raw=False)
            if exception.code == ExceptionCode.USER_EXISTS:
                logging.error(msg=exception.msg)
                print("Sorry, that username is taken, please choose another one")
            else:
                logging.fatal(msg=exception.msg)
                print("Sorry, something went wrong")
                client_send_socket.close()
                client_recv_socket.close()
                sys.exit(1)
        else:
            print("Successfully registered")
        
        # Start threads for sending and receiving messages
        send_thread = threading.Thread(target=send_handler)
        receive_thread = threading.Thread(target=receive_handler)
        send_thread.start()
        receive_thread.start()
        send_thread.join()
        receive_thread.join()
    except (KeyboardInterrupt, EOFError, SystemExit):
        sys.exit(0)
    except:
        logging.fatal(msg=sys.exc_info()[0])
        sys.exit(1)
