import logging
import select
import socket

import msgpack

from exceptions import ExceptionCode, RequestException

# Configure logging
logging.basicConfig(filename="./client.log", level=logging.DEBUG)

# Constants for header lengths and formats
HEADER_TYPE_LENGTH = 1
HEADER_MESSAGE_LENGTH = 7
SERVER_IP = "127.0.0.1"
SERVER_PORT = 1234
ENCODING_FORMAT = "utf-8"

# Client settings
CLIENT_IP = "127.0.0.1"
CLIENT_SEND_PORT = 5670
CLIENT_RECV_PORT = 4323

# Get the username from the user
user_name = input("Enter username: ")
client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # Send socket
client_recv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # Receive socket

client_send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_send_socket.bind((CLIENT_IP, CLIENT_SEND_PORT))
client_recv_socket.bind((CLIENT_IP, CLIENT_RECV_PORT))

client_send_socket.connect((SERVER_IP, SERVER_PORT))
client_recv_socket.listen(5)

# Encode the username and prepare the header
encoded_username = user_name.encode(ENCODING_FORMAT)
username_header = f"n{len(encoded_username):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
client_send_socket.send(username_header + encoded_username)

receiving = False
connected = [client_recv_socket]

def handle_sending():
    global client_send_socket
    recipient_username = input("Enter recipient's username: ")
    if recipient_username:
        # Encode the recipient's username and prepare the header
        encoded_recipient = recipient_username.encode(ENCODING_FORMAT)
        request_header = f"r{len(encoded_recipient):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
        logging.debug(f"Sent packet {(request_header + encoded_recipient).decode(ENCODING_FORMAT)}")
        client_send_socket.send(request_header + encoded_recipient)
        
        # Read response type
        response_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
        logging.log(level=logging.DEBUG, msg=f"Response type: {response_type}")
        
        # Read response length and response
        response_length = int(client_send_socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT).strip())
        response = client_send_socket.recv(response_length)
        
        if response_type == "r":
            # If response type is 'r', unpack recipient address and connect to it
            recipient_addr = msgpack.unpackb(response, use_list=False)
            logging.log(level=logging.DEBUG, msg=f"Response: {recipient_addr}")
            
            # Close and reopen the send socket to connect to the recipient
            client_send_socket.close()
            client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_send_socket.connect((recipient_addr[0], CLIENT_RECV_PORT))
            
            # Send a test message
            msg = "Test message".encode(ENCODING_FORMAT)
            header = f"m{len(msg):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
            client_send_socket.send(header + msg)
        elif response_type == "e":
            # If response type is 'e', unpack the error message
            error: RequestException = msgpack.unpackb(response, object_hook=RequestException.from_dict, raw=False)
            logging.log(level=logging.ERROR, msg=error)

def receive_message(socket: socket.socket) -> str:
    # Read message type
    message_type = socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
    if not message_type:
        raise RequestException(
            msg=f"Peer at {socket.getpeername()} closed the connection",
            code=ExceptionCode.DISCONNECT,
        )
    elif message_type != "m":
        raise RequestException(
            msg="Invalid message type in header",
            code=ExceptionCode.INVALID_HEADER,
        )
    else:
        # Read message length and message content
        message_len = int(socket.recv(HEADER_MESSAGE_LENGTH).decode(ENCODING_FORMAT))
        return socket.recv(message_len).decode(ENCODING_FORMAT)

def handle_receiving():
    global client_send_socket, client_recv_socket, user_name

    read_sockets, _, __ = select.select(connected, [], [])
    for notified_socket in read_sockets:
        if notified_socket == client_recv_socket:
            # Accept new connection from peer
            peer_socket, peer_addr = client_recv_socket.accept()
            # peer_addr is {ip, port} of the incoming connection
            logging.log(
                level=logging.DEBUG,
                msg=(
                    "Accepted new connection from"
                    f" {peer_addr[0]}:{peer_addr[1]}"
                ),
            )
            try:
                connected.append(peer_socket)
                #lookup is peer_addr[0] which is the ip address of the incoming connection
                lookup: bytes = peer_addr[0].encode(ENCODING_FORMAT)
                header = f"l{len(lookup):<{HEADER_MESSAGE_LENGTH}}".encode(ENCODING_FORMAT)
                logging.debug(f"Sending packet {(header + lookup).decode(ENCODING_FORMAT)}")
                client_send_socket.send(header + lookup)
                # Receive response type
                res_type = client_send_socket.recv(HEADER_TYPE_LENGTH).decode(ENCODING_FORMAT)
                if res_type not in ["l", "e"]:
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
                    if res_type == "l":
                        # Log the username of the incoming connection
                        user_name = response.decode(ENCODING_FORMAT)
                        logging.info(
                            f"Username {user_name} is trying to send a message"
                        )
                    else:
                        exception = msgpack.unpackb(
                            response,
                            object_hook=RequestException.from_dict,
                            raw=False,
                        )
                        logging.error(exception)
                        raise exception
            except RequestException as e:
                logging.log(level=logging.ERROR, msg=e)
                return
        else:
            try:
                # Receive and log the message
                msg: str = receive_message(notified_socket)
                logging.info(f"Received message {msg} from {user_name}")
            except RequestException as e:
                if e.code == ExceptionCode.DISCONNECT:
                    try:
                        connected.remove(notified_socket)
                    except ValueError:
                        logging.info("already removed")
                logging.log(level=logging.ERROR, msg=f"Exception: {e.msg}")
                return

while True:
    handle_sending()
    handle_receiving()
