# Imports (standard libraries)
import hashlib
import logging
import os
import pickle
import select
import shutil
import socket
import sys
import time
from datetime import datetime
from io import BufferedReader
from pathlib import Path
from pprint import pformat

# Imports (PyPI)
import msgpack
from notifypy import Notify
from PyQt5.QtCore import (
    QCoreApplication,
    QMetaObject,
    QMutex,
    QObject,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThread,
    QThreadPool,
    pyqtSignal,
)
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Imports (UI components)
from ui.ErrorDialog import Ui_ErrorDialog
from ui.FileInfoDialog import Ui_FileInfoDialog
from ui.FileProgressWidget import Ui_FileProgressWidget
from ui.FileSearchDialog import Ui_FileSearchDialog
from ui.SettingsDialog import Ui_SettingsDialog

# Imports (utilities)
sys.path.append("../")
from client.app import MainWindow
from utils.constants import (
    CLIENT_RECV_PORT,
    CLIENT_SEND_PORT,
    FILE_BUFFER_LEN,
    FMT,
    HEADER_MSG_LEN,
    HEADER_TYPE_LEN,
    HEARTBEAT_TIMER,
    LEADING_HTML,
    ONLINE_TIMEOUT,
    SERVER_RECV_PORT,
    TEMP_FOLDER_PATH,
    TRAILING_HTML,
)
from utils.exceptions import ExceptionCode, RequestException
from utils.helpers import (
    construct_message_html,
    convert_size,
    generate_transfer_progress,
    get_directory_size,
    get_file_hash,
    get_files_in_dir,
    get_unique_filename,
    import_file_to_share,
    path_to_dict,
)
from utils.socket_functions import get_self_ip, recvall, request_ip, request_uname, update_share_data
from utils.types import (
    CompressionMethod,
    DBData,
    DirData,
    DirProgress,
    FileMetadata,
    FileRequest,
    HeaderCode,
    ItemSearchResult,
    Message,
    ProgressBarData,
    TransferProgress,
    TransferStatus,
    UpdateHashParams,
    UserSettings,
)

# Global constants
SERVER_IP = ""
SERVER_ADDR = ()
CLIENT_IP = get_self_ip()

# Logging configuration
logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.DEBUG,
    handlers=[
        logging.FileHandler(
            f"{str(Path.home())}/.Flux/logs/client_{datetime.now().strftime('%d-%m-%Y_%H-%M-%S')}.log"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
# socket to connect to main server
client_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# socket to receive new connections from peers
client_recv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Configuring socket options to reuse addresses and immediately transmit data
client_send_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
client_send_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
client_recv_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

# Binding sockets
client_send_socket.bind((CLIENT_IP, CLIENT_SEND_PORT))
client_recv_socket.bind((CLIENT_IP, CLIENT_RECV_PORT))

# Make client receive socket listen for new connections
client_recv_socket.listen(5)

# Global variables

# List of peer sockets connected to the clients
connected = [client_recv_socket]
# Mapping from username to last seen timestamp
uname_to_status: dict[str, int] = {}
# Mapping from username to list of messages
messages_store: dict[str, list[Message]] = {}
# Username selected by the user in the list
selected_uname: str = ""
# List of file items chosen by the user for downloading
selected_file_items: list[DirData] = []
# The user's own username
self_uname: str = ""
# Mapping from file path to download status and progress
transfer_progress: dict[Path, TransferProgress] = {}
# Mapping from file path to progress bar displaying its status
progress_widgets: dict[Path, Ui_FileProgressWidget] = {}
# Mapping from directory path to cumulative download status and progress
dir_progress: dict[Path, DirProgress] = {}
# The settings of the current session
user_settings: UserSettings = {}
# Cache to lookup username of a given IP
uname_to_ip: dict[str, str] = {}
# Cache to lookup IP of a given username
ip_to_uname: dict[str, str] = {}
# Whether or not an error dialog is already open on the screen currently
error_dialog_is_open = False
# A mutex to prevent race conditions while sending data to the server socket from different threads
server_socket_mutex = QMutex()


def show_error_dialog(error_msg: str, show_settings: bool = False) -> None:
    """Displays an error dialog with the given message
    Parameters
    ----------
    error_msg : str
        The error message to be displayed in the dialog
    show_settings : bool, optional
        A flag used to indicate whether or not to display the settings button (default is False)
    """

    global user_settings
    global error_dialog_is_open

    error_dialog = QDialog()
    error_dialog.ui = Ui_ErrorDialog(error_dialog, error_msg, user_settings if show_settings else None)

    # Don't display dialog if another dialog is already open
    if not error_dialog_is_open:
        error_dialog_is_open = True
        error_dialog.exec()
        error_dialog_is_open = False


class SaveProgressWorker(QObject):
    """A worker that periodically saves the download progress and statuses to a file
    Methods
    -------
    dump_progress_data()
        Stores the current download progress and statuses of all files and folders to a file
    run()
        Runs the dump_progress_data function every 10 seconds
    """

    def dump_progress_data(self) -> None:
        """Pickles the transfer_progress, dir_progress and progress_widgets dictionaries into 3 files"""
        global transfer_progress
        global dir_progress
        global progress_widgets

        for path in transfer_progress.keys():
            if transfer_progress[path]["status"] in [
                TransferStatus.DOWNLOADING,
                TransferStatus.NEVER_STARTED,
            ]:
                transfer_progress[path]["status"] = TransferStatus.PAUSED

        # Pickle the transfer_progress dictionary
        with (Path.home() / ".Flux/db/transfer_progress.pkl").open(mode="wb") as transfer_progress_dump:
            logging.debug(msg="Created transfer progress dump")
            pickle.dump(transfer_progress, transfer_progress_dump)

        # Pickle the dir_progress dictionary
        with (Path.home() / ".Flux/db/dir_progress.pkl").open(mode="wb") as dir_progress_dump:
            logging.debug(msg="Created dir progress dump")
            dir_progress_writeable: dict[Path, DirProgress] = {}
            for path in dir_progress.keys():
                dir_progress[path]["mutex"].lock()
                dir_progress_writeable[path] = {
                    "current": dir_progress[path]["current"],
                    "total": dir_progress[path]["total"],
                    "status": dir_progress[path]["status"],
                }
                dir_progress[path]["mutex"].unlock()
            pickle.dump(dir_progress_writeable, dir_progress_dump)

        # Pickle the progress_widgets dictionary
        with (Path.home() / ".Flux/db/progress_widgets.pkl").open(mode="wb") as progress_widgets_dump:
            progress_widgets_writeable: dict[Path, ProgressBarData] = {}
            for path, widget in progress_widgets.items():
                progress_widgets_writeable[path] = {
                    "current": widget.ui.progressBar.value(),
                    "total": widget.ui.total,
                }
            pickle.dump(progress_widgets_writeable, progress_widgets_dump)

    def run(self):
        """Runs the dump_progress_data periodically"""
        global transfer_progress
        global dir_progress
        global progress_widgets

        # Save the progress data every 10 seconds
        while True:
            self.dump_progress_data()
            time.sleep(10)


class HeartbeatWorker(QObject):
    """A worker that periodically sends heartbeat messages to the server
    Attributes
    ----------
    update_status : pyqtSignal
        A signal that is emitted every time new status data is obtained from the server
    Methods
    -------
    run()
        Sends heartbeat messages to the server periodically, every HEARTBEAT_TIMER seconds
    """

    update_status = pyqtSignal(dict)

    def run(self):
        """Sends a heartbeat message to the server every HEARTBEAT_TIMER seconds and updates the statuses of all users"""
        global client_send_socket
        global server_socket_mutex

        # Encode and send the heartbeat message
        heartbeat = HeaderCode.HEARTBEAT_REQUEST.value.encode(FMT)
        while True:
            server_socket_mutex.lock()
            client_send_socket.send(heartbeat)
            type = client_send_socket.recv(HEADER_TYPE_LEN).decode(FMT)
            if type == HeaderCode.HEARTBEAT_REQUEST.value:
                # Receive updated user statuses from the server and emit the update_status signal
                length = int(client_send_socket.recv((HEADER_MSG_LEN)).decode(FMT))
                new_status = msgpack.unpackb(client_send_socket.recv(length))
                server_socket_mutex.unlock()
                self.update_status.emit(new_status)
                time.sleep(HEARTBEAT_TIMER)
            else:
                server_socket_mutex.unlock()
                logging.error(
                    f"Server sent invalid message type in header: {type}",
                )
                sys.exit(
                    show_error_dialog(
                        "An error occurred while communicating with the server.\nTry reconnecting or check the server logs.",
                        True,
                    )
                )


class ReceiveDirectTransferWorker(QRunnable):
    """A worker that receives files sent via Direct Transfer from a peer
    Attributes
    ----------
    metadata : FileMetadata
        The metadata of the file to be downloaded
    sender : str
        The username of the sender
    file_receive_socket : socket.socket
        The socket on which to receive the file
    Methods
    -------
    run()
        Receives the file from the sender on the file_receive_socket socket
    """

    def __init__(self, metadata: FileMetadata, sender: str, file_receive_socket: socket.socket):
        super().__init__()
        logging.debug("file recv worker init")
        self.metadata = metadata
        self.sender = sender
        self.file_recv_socket = file_receive_socket
        self.signals = Signals()

    def run(self):
        """Receives the file with the given metadata from the sender on the file_receive_socket socket"""
        global user_settings
        global transfer_progress

        try:
            # Accept connection from the sender
            sender, _ = self.file_recv_socket.accept()
            # Temporary path to write the file to while the download is not complete
            temp_path: Path = TEMP_FOLDER_PATH / self.sender / self.metadata["path"]
            # Final download path in the user's download folder to move the file to after the download is complete
            final_download_path: Path = get_unique_filename(
                Path(user_settings["downloads_folder_path"]) / self.sender / self.metadata["path"],
            )
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            final_download_path.parent.mkdir(parents=True, exist_ok=True)

            # Initialize transfer progress with default values
            transfer_progress[temp_path] = {
                "status": TransferStatus.DOWNLOADING,
                "progress": 0,
                "percent_progress": 0.0,
            }

            logging.debug(msg="Obtaining file")
            # Check if there is sufficient disk space available to receive the file
            if shutil.disk_usage(user_settings["downloads_folder_path"]).free > self.metadata["size"]:
                with temp_path.open(mode="wb") as file_to_write:
                    byte_count = 0
                    hash = hashlib.sha1()
                    self.signals.receiving_new_file.emit((temp_path, self.metadata["size"]))
                    while True:
                        logging.debug(msg="Obtaining file chunk")

                        # Receive a file chunk
                        file_bytes_read: bytes = sender.recv(FILE_BUFFER_LEN)

                        # Update cumulative file hash
                        hash.update(file_bytes_read)
                        num_bytes_read = len(file_bytes_read)
                        byte_count += num_bytes_read
                        transfer_progress[temp_path]["progress"] = byte_count

                        # Write chunk to temp file
                        file_to_write.write(file_bytes_read)

                        # Emit a signal to update the progress bar
                        self.signals.file_progress_update.emit(temp_path)

                        # If there are no more chunks being sent, terminate the transfer
                        if num_bytes_read == 0:
                            break

                    received_hash = hash.hexdigest()
                    # Compare hash of received file with hash given in the metadata
                    if received_hash == self.metadata["hash"]:
                        transfer_progress[temp_path]["status"] = TransferStatus.COMPLETED
                        final_download_path.parent.mkdir(parents=True, exist_ok=True)

                        # Move the file to the final download path in the user's download folder
                        shutil.move(temp_path, final_download_path)
                        print("Succesfully received 1 file")

                        # Emit a signal to delete the progress bar
                        self.signals.file_download_complete.emit(temp_path)
                        del transfer_progress[temp_path]
                    else:
                        transfer_progress[temp_path]["status"] = TransferStatus.FAILED
                        logging.error(msg=f"Failed integrity check for file {self.metadata['path']}")
                        show_error_dialog(f"Failed integrity check for file {self.metadata['path']}.")
        except Exception as e:
            logging.exception(msg=f"Failed to receive file: {e}")
            show_error_dialog(f"Failed to receive file:\n{e}")
        finally:
            # Close the connection with the sender
            self.file_recv_socket.close()


class HandleFileRequestWorker(QRunnable):
    """A worker that handles incoming requests to download files from other peers
    Attributes
    ----------
    filepath : Path
        The path to the file to be downloaded
    requester : tuple[str, int]
        The address of the requester, specified of a tuple of IP address and port number
    request_hash : bool
        Whether or not to send the hash of the file to the peer and the server
    resume_offset : int
        Number of bytes of offset to send the file from
    Methods
    -------
    run()
        Sends the requested file at the filepath to the requester
    """

    def __init__(self, filepath: Path, requester: tuple[str, int], request_hash: bool, resume_offset: int):
        super().__init__()
        self.filepath = filepath
        self.requester = requester
        self.request_hash = request_hash
        self.resume_offset = resume_offset

    def run(self) -> None:
        """Send the file at filepath if it exists to the requester at the given address after opening a new port"""

        # Open a new socket for sending the file
        file_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        file_send_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            # Attempt to connect to the requester at the given address
            file_send_socket.connect(self.requester)
        except Exception as e:
            logging.exception(f"Exception when sending file: {e}")
            return
        try:
            hash = ""

            # If request_hash is set, compute the hash of the file before sending it
            if self.request_hash:
                hash = get_file_hash(str(self.filepath))

            # Create the file metadata, encode it and send it to the requester
            filemetadata: FileMetadata = {
                "path": str(self.filepath).removeprefix(user_settings["share_folder_path"] + "/"),
                "size": self.filepath.stat().st_size,
                "hash": hash if self.request_hash else None,
            }

            logging.debug(filemetadata)
            filemetadata_bytes = msgpack.packb(filemetadata)
            filesend_header = f"{HeaderCode.DIRECT_TRANSFER.value}{len(filemetadata_bytes):<{HEADER_MSG_LEN}}".encode(
                FMT
            )

            with self.filepath.open(mode="rb") as file_to_send:
                logging.debug(f"Sending file {filemetadata['path']} to {self.requester}")
                # Send the file metadata to the requester
                file_send_socket.send(filesend_header + filemetadata_bytes)

                total_bytes_read = 0

                if os.name == "posix":
                    # On Unix-like systems use the high-performance socket.sendfile function with the resume_offset
                    file_send_socket.sendfile(file_to_send, self.resume_offset)
                else:
                    # On other systems
                    # Seek to the point in the file after the resume_offset
                    file_to_send.seek(self.resume_offset)

                    # Send file chunks until the whole file is sent
                    while total_bytes_read != filemetadata["size"] - self.resume_offset:
                        bytes_read = file_to_send.read(FILE_BUFFER_LEN)
                        num_bytes = file_send_socket.send(bytes_read)
                        total_bytes_read += num_bytes
                print("\nFile Sent")
            if self.request_hash:
                # If the requester set the request_hash option, the server does not have the hash of the file
                # so send the updated hash to the server
                update_hash_params: UpdateHashParams = {
                    "filepath": str(self.filepath).removeprefix(user_settings["share_folder_path"] + "/"),
                    "hash": hash,
                }
                update_hash_bytes = msgpack.packb(update_hash_params)
                update_hash_header = f"{HeaderCode.UPDATE_HASH.value}{len(update_hash_bytes):<{HEADER_MSG_LEN}}".encode(
                    FMT
                )
                server_socket_mutex.lock()
                client_send_socket.send(update_hash_header + update_hash_bytes)
                server_socket_mutex.unlock()
        except Exception as e:
            logging.exception(f"File Sending failed: {e}")
            show_error_dialog(f"File Sending failed.\n{e}")
        finally:
            file_send_socket.close()
            

class ReceiveHandler(QObject):
    """A worker that handles incoming packets sent by connected peers
    Attributes
    ----------
    message_received : pyqtSignal
        A signal that is emitted every time a message is received from a peer
    file_incoming : pyqtSignal
        A signal that is emitted every time a file is being received from a peer
    send_file_pool : QThreadPool
        The global thread pool used to run SendFileWorker instances
    Methods
    -------
    receive_msg(socket: socket.socket)
        Handle receiving incoming file requests, messages and Direct Transfer requests
    run()
        Accept new incoming connections from peers and execute receive_msg for each peer
    """

    message_received = pyqtSignal(dict)
    file_incoming = pyqtSignal(tuple)
    send_file_pool = QThreadPool.globalInstance()

    def receive_msg(self, socket: socket.socket) -> str | None:
        """Receives incoming messages, file requests or Direct Transfer requests
        Parameters
        ----------
        socket : socket.socket
            The socket on which to receive the message
        Returns
        ----------
        str | None
            Returns the message received from the peer, or None in case of an exception
        Raises
        ----------
        RequestException
            In case of any exceptions that occur in receiving the message
        """

        global client_send_socket
        global user_settings

        # Receive message type
        logging.debug(f"Receiving from {socket.getpeername()}")
        message_type = socket.recv(HEADER_TYPE_LEN).decode(FMT)
        if not len(message_type):
            raise RequestException(
                msg=f"Peer at {socket.getpeername()} closed the connection",
                code=ExceptionCode.DISCONNECT,
            )
        elif message_type not in [
            HeaderCode.MESSAGE.value,
            HeaderCode.DIRECT_TRANSFER.value,
            HeaderCode.DIRECT_TRANSFER_REQUEST.value,
            HeaderCode.FILE_REQUEST.value,
        ]:
            raise RequestException(
                msg=f"Invalid message type in header. Received [{message_type}]",
                code=ExceptionCode.INVALID_HEADER,
            )
        else:
            match message_type:
                # Direct Transfer
                case HeaderCode.DIRECT_TRANSFER.value:
                    # Receive file metadata
                    file_header_len = int(socket.recv(HEADER_MSG_LEN).decode(FMT))
                    file_header: FileMetadata = msgpack.unpackb(socket.recv(file_header_len))
                    logging.debug(msg=f"receiving file with metadata {file_header}")

                    # Final path to store the file in
                    write_path: Path = get_unique_filename(
                        Path(user_settings["downloads_folder_path"]) / file_header["path"],
                    )
                    try:
                        file_to_write = open(str(write_path), "wb")
                        logging.debug(f"Creating and writing to {write_path}")
                        try:
                            byte_count = 0

                            # Keep receiving file chunks until the entire file is received
                            while byte_count != file_header["size"]:
                                file_bytes_read: bytes = socket.recv(FILE_BUFFER_LEN)
                                byte_count += len(file_bytes_read)
                                file_to_write.write(file_bytes_read)
                            file_to_write.close()
                            return f"Received file {write_path.name}"
                        except Exception as e:
                            logging.exception(e)
                            # TODO: add status bar message here, show error in progress bar
                            return None
                    except Exception as e:
                        logging.exception(e)
                        # TODO: add status bar message here, show error in progress bar
                        return None
                # Incoming file request
                case HeaderCode.FILE_REQUEST.value:
                    # Receive file request
                    req_header_len = int(socket.recv(HEADER_MSG_LEN).decode(FMT))
                    file_req_header: FileRequest = msgpack.unpackb(socket.recv(req_header_len))
                    logging.debug(msg=f"Received request: {file_req_header}")
                    requested_file_path = Path(user_settings["share_folder_path"]) / file_req_header["filepath"]

                    # Check if the requested file exists and is a file
                    if requested_file_path.is_file():
                        socket.send(HeaderCode.FILE_REQUEST.value.encode(FMT))

                        # Spawn new HandleFileRequestWorker worker to transmit the file
                        send_file_handler = HandleFileRequestWorker(
                            requested_file_path,
                            (socket.getpeername()[0], file_req_header["port"]),
                            file_req_header["request_hash"],
                            file_req_header["resume_offset"],
                        )
                        self.send_file_pool.start(send_file_handler)
                        return None
                    # If the requested file exists and is a directory
                    elif requested_file_path.is_dir():
                        raise RequestException(
                            f"Requested a directory, {file_req_header['filepath']} is not a file.",
                            ExceptionCode.BAD_REQUEST,
                        )
                    # If the requested file does not exist
                    else:
                        # Update the share data on the server again with the latest information
                        share_data = msgpack.packb(
                            path_to_dict(
                                Path(user_settings["share_folder_path"]),
                                user_settings["share_folder_path"],
                            )["children"]
                        )
                        share_data_header = f"{HeaderCode.SHARE_DATA.value}{len(share_data):<{HEADER_MSG_LEN}}".encode(
                            FMT
                        )
                        server_socket_mutex.lock()
                        client_send_socket.sendall(share_data_header + share_data)
                        server_socket_mutex.unlock()
                        raise RequestException(
                            f"Requested file {file_req_header['filepath']} is not available",
                            ExceptionCode.NOT_FOUND,
                        )
                # Incoming Direct Transfer request
                case HeaderCode.DIRECT_TRANSFER_REQUEST.value:
                    metadata_len = int(socket.recv(HEADER_MSG_LEN).decode(FMT))
                    metadata: FileMetadata = msgpack.unpackb(socket.recv(metadata_len))
                    # Emit the file_incoming signal
                    self.file_incoming.emit((metadata, socket))
                # Incoming message
                case _:
                    message_len = int(socket.recv(HEADER_MSG_LEN).decode(FMT))
                    # Return the received message
                    return recvall(socket, message_len).decode(FMT)

    def run(self):
        """Receives new connections and handles them"""

        global messages_store
        global client_send_socket
        global client_recv_socket
        global connected
        global server_socket_mutex

        while True:
            read_sockets: list[socket.socket]
            # Use the select system call to get a list of sockets that are ready for receiving
            read_sockets, _, __ = select.select(connected, [], [])
            for notified_socket in read_sockets:
                # New incoming connection
                if notified_socket == client_recv_socket:
                    # Accept the connection
                    peer_socket, peer_addr = client_recv_socket.accept()
                    logging.debug(
                        msg=f"Accepted new connection from {peer_addr[0]}:{peer_addr[1]}",
                    )
                    try:
                        # Lookup the username of the peer in the cache
                        if ip_to_uname.get(peer_addr[0]) is None:
                            # In case of a cache miss, lookup the username from the server
                            server_socket_mutex.lock()
                            peer_uname = request_uname(peer_addr[0], client_send_socket)
                            server_socket_mutex.unlock()
                            if peer_uname is not None:
                                # Cache the username for future use
                                ip_to_uname[peer_addr[0]] = peer_uname
                        # Add the socket to the list of connected peers
                        connected.append(peer_socket)
                    except Exception as e:
                        logging.exception(msg=e)
                        show_error_dialog("Error occured when obtaining peer data.\n{e}")
                        break
                else:
                    # Incoming packet from a connected peer
                    try:
                        # Lookup the username in the cache
                        username = ip_to_uname[notified_socket.getpeername()[0]]
                        # Handle the incoming packet
                        message_content: str = self.receive_msg(notified_socket)
                        # If receive_msg returns a string, it is a message to be displayed in the message area
                        if message_content:
                            message: Message = {"sender": username, "content": message_content}
                            # Check if desktop notifications are enabled in the settings
                            if user_settings["show_notifications"] and username != selected_uname:
                                # Fire a notification with the message as the payload
                                notif = Notify()
                                notif.application_name = "Flux"
                                notif.title = "Message"
                                notif.message = f"{username}: {message_content}"
                                notif.send()
                            # Store the message in the messages_store
                            messages_store.get(username, []).append(message)
                            # Emit the message_received signal to update the message area
                            self.message_received.emit(message)
                    except RequestException as e:
                        # Remove disconnected peers from the list of connected peers
                        if e.code == ExceptionCode.DISCONNECT:
                            try:
                                connected.remove(notified_socket)
                            except ValueError:
                                logging.info("already removed")
                        logging.error(msg=f"Exception: {e.msg}")
                        # show_error_dialog(f"Error occurred when communicating with peer.\n{e.msg}")
                        break
                    except Exception as e:
                        logging.exception(f"Error communicating with peer: {e}")


class SendFileWorker(QObject):
    """A worker that sends files (for Direct Transfer) to peers
    Attributes
    ----------
    sending_file : pyqtSignal
        A signal that is emitted when the file is being sent
    completed : pyqtSignal
        A signal that is emitted when the file transfer is complete
    filepath : Path
        The path to the file to be sent
    Methods
    -------
    run()
        Sends the file at filepath to the selected peer
    """

    sending_file = pyqtSignal(dict)
    completed = pyqtSignal()

    def __init__(self, filepath: Path):
        global client_send_socket
        global server_socket_mutex
        super().__init__()

        self.filepath = filepath
        self.peer_ip = ""

        # Lookup the IP of the selected username in the cache
        if uname_to_ip.get(selected_uname) is None:
            # In case of a cache miss, request the IP from the server and store it in the cache
            server_socket_mutex.lock()
            self.peer_ip = request_ip(selected_uname, client_send_socket)
            server_socket_mutex.unlock()
            uname_to_ip[selected_uname] = self.peer_ip
        else:
            self.peer_ip = uname_to_ip.get(selected_uname)

        if self.peer_ip is not None:
            # Open a new socket to connect to the peer
            self.client_peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_peer_socket.connect((self.peer_ip, CLIENT_RECV_PORT))

    def run(self):
        """Sends the file at filepath to the selected peer"""

        global self_uname
        # If the file at filepath exists and is a file
        if self.filepath and self.filepath.is_file():
            logging.debug(f"{self.filepath} chosen to send")
            filemetadata: FileMetadata = {
                "path": self.filepath.name,
                "size": self.filepath.stat().st_size,
                "hash": get_file_hash(str(self.filepath)),
            }
            logging.debug(filemetadata)
            filemetadata_bytes = msgpack.packb(filemetadata)
            logging.debug(filemetadata_bytes)
            filesend_header = f"{HeaderCode.DIRECT_TRANSFER_REQUEST.value}{len(filemetadata_bytes):<{HEADER_MSG_LEN}}"
            filesend_header = filesend_header.encode(FMT)

            # Open a new socket to transmit the file
            file_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            file_to_send: BufferedReader
            try:
                logging.debug(f"Sending file {self.filepath} to {selected_uname}")

                # Send the file metadata to the peer
                self.client_peer_socket.send(filesend_header + filemetadata_bytes)

                response_type = self.client_peer_socket.recv(HEADER_TYPE_LEN).decode(FMT)
                logging.debug(f"Received header {response_type}")
                if response_type == HeaderCode.DIRECT_TRANSFER_REQUEST.value:
                    response_len = int(self.client_peer_socket.recv(HEADER_MSG_LEN).decode(FMT).strip())
                    logging.debug(f"Received len {response_len}")
                    port = int(self.client_peer_socket.recv(response_len).decode(FMT))
                    logging.debug(f"Received port {port}")

                    # If the recipient acknowledges the transfer, a valid port is sent
                    if port != -1:
                        # Connect to the recipient on the given port
                        file_send_socket.connect((self.peer_ip, port))
                        with self.filepath.open(mode="rb") as file_to_send:
                            total_bytes_read = 0
                            msg = f"Sending file {str(self.filepath)}"
                            if messages_store.get(selected_uname) is not None:
                                messages_store[selected_uname].append({"sender": self_uname, "content": msg})
                            else:
                                messages_store[selected_uname] = [{"sender": self_uname, "content": msg}]

                            # Emit the sending_file signal
                            self.sending_file.emit({"sender": self_uname, "content": msg})
                            if os.name == "posix":
                                # On Unix-like systems use the high performance socket.sendfile method
                                file_send_socket.sendfile(file_to_send)
                            else:
                                # On other systems
                                # Send file chunks until the entire file is sent
                                while total_bytes_read != filemetadata["size"]:
                                    logging.debug(f'sending file bytes {total_bytes_read} of {filemetadata["size"]}')
                                    bytes_read = file_to_send.read(FILE_BUFFER_LEN)
                                    file_send_socket.sendall(bytes_read)
                                    num_bytes = len(bytes_read)
                                    total_bytes_read += num_bytes
                            print("\nFile Sent")
            except Exception as e:
                logging.exception(f"Direct transfer failed: {e}")
                show_error_dialog(f"Failed to send file.\n{e}")
            finally:
                file_send_socket.close()

        else:
            logging.error(f"{self.filepath} not found")
            show_error_dialog("Selected file does not exist.")
            print(
                f"\nUnable to perform send request\
                ensure that the file is available in {user_settings['share_folder_path']}"
            )
        # Emit the completed event
        self.completed.emit()


class Signals(QObject):
    """A class containing signals that are emitted for various events
    Attributes
    ----------
    start_download : pyqtSignal
        A signal that is emitted when a download starts from the search dialog, to create a progress bar
    receiving_new_file : pyqtSignal
        A signal that is emitted when the a new file download starts, to create a progress bar
    file_progress_update : pyqtSignal
        A signal that is emitted when a file transfer progress is updated, to update the progress bar
    dir_progress_update : pyqtSignal
        A signal that is emitted when a directory transfer progress is updated, to update the progress bar
    pause_download : pyqtSignal
        A signal that is emitted when a file or directory transfer is paused
    resume_download : pyqtSignal
        A signal that is emitted when a file or directory transfer is resumed
    file_download_complete : pyqtSignal
        A signal that is emitted when a file or directory transfer is completed
    """

    dir_progress_update = pyqtSignal(tuple)
    file_download_complete = pyqtSignal(Path)
    file_progress_update = pyqtSignal(Path)
    pause_download = pyqtSignal(Path)
    receiving_new_file = pyqtSignal(tuple)
    resume_download = pyqtSignal(Path)
    start_download = pyqtSignal(dict)


class RequestFileWorker(QRunnable):
    """A worker that requests files or directories to be downloaded from the selected peer
    Attributes
    ----------
    file_item : DirData
        The metadata of the file item to be requested
    peer_ip : str
        The IP of the sender to request the file from
    sender : str
        The username of the sender
    parent_dir : Path | None
        In case of a directory download, the path of the parent directory to which the file_item belongs
    Methods
    ----------
    run()
        Requests the file item from the sender
    """

    def __init__(self, file_item: DirData, peer_ip: str, sender: str, parent_dir: Path | None) -> None:
        super().__init__()
        self.file_item = file_item
        self.peer_ip = peer_ip
        self.sender = sender
        self.parent_dir = parent_dir
        self.signals = Signals()

        logging.debug(msg=f"Thread worker for requesting {sender}/{file_item['name']}")

    def run(self) -> None:
        """Requests the file_item from the sender at peer_ip
        Raises
        ----------
        RequestException
            In case of any errors that occur during the transfer
        """

        global transfer_progress
        global user_settings

        try:
            # Open a new socket to request the file and connect to the sender
            self.client_peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client_peer_socket.connect((self.peer_ip, CLIENT_RECV_PORT))

            # Open a new socket to listen for the incoming file transfer from the sender
            file_recv_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            file_recv_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            file_recv_socket.bind((CLIENT_IP, 0))
            file_recv_socket.listen()

            # Get the port of the file receive socket to send to the sender
            file_recv_port = file_recv_socket.getsockname()[1]

            offset = 0
            temp_path = TEMP_FOLDER_PATH / self.sender / self.file_item["path"]
            logging.debug(f"Using temp path {str(temp_path)}")
            # Check if an incomplete download already exists at the temp path (in case of resuming downloads)
            if temp_path.exists():
                # If such a file exists, then request an offset of the number of bytes already downloaded
                offset = temp_path.stat().st_size

            logging.debug(f"Offset of {offset} bytes")

            # Optimization for small files
            # If a file can be transmitted in a single packet (<=16 kB) then don't request the hash
            is_tiny_file = self.file_item["size"] <= FILE_BUFFER_LEN

            # Don't request the hash if it is already available or is a small file
            request_hash = self.file_item["hash"] is None and not is_tiny_file
            file_request: FileRequest = {
                "port": file_recv_port,
                "filepath": self.file_item["path"],
                "request_hash": request_hash,
                "resume_offset": offset,
            }

            file_req_bytes = msgpack.packb(file_request)
            file_req_header = f"{HeaderCode.FILE_REQUEST.value}{len(file_req_bytes):<{HEADER_MSG_LEN}}".encode(FMT)

            # Send the file request to the sender
            self.client_peer_socket.send(file_req_header + file_req_bytes)
            res_type = self.client_peer_socket.recv(HEADER_TYPE_LEN).decode(FMT)
            logging.debug(f"received header type {res_type} from sender")
            match res_type:
                # If the sender has the file
                case HeaderCode.FILE_REQUEST.value:
                    # Accept the new connection from the sender on the file receive port
                    sender, _ = file_recv_socket.accept()
                    logging.debug(msg=f"Sender tried to connect: {sender.getpeername()}")
                    res_type = sender.recv(HEADER_TYPE_LEN).decode(FMT)
                    if res_type == HeaderCode.DIRECT_TRANSFER.value:
                        file_header_len = int(sender.recv(HEADER_MSG_LEN).decode(FMT))
                        file_header: FileMetadata = msgpack.unpackb(sender.recv(file_header_len))
                        logging.debug(msg=f"receiving file with metadata {file_header}")

                        # Check if sufficient free disk space is available to receive the file
                        if shutil.disk_usage(user_settings["downloads_folder_path"]).free > file_header["size"]:
                            # Final path in the user's download folder to move the file to after downloading
                            final_download_path: Path = get_unique_filename(
                                Path(user_settings["downloads_folder_path"]) / file_header["path"],
                            )
                            try:
                                temp_path.parent.mkdir(parents=True, exist_ok=True)
                                file_to_write = temp_path.open("ab")
                                logging.debug(f"Creating and writing to {temp_path}")
                                try:
                                    byte_count = 0
                                    hash = hashlib.sha1()
                                    # Initialize the transfer progress of the file item
                                    if transfer_progress.get(temp_path) is None:
                                        transfer_progress[temp_path] = {}

                                    # If the download is not paused, set the status to DOWNLOADING
                                    if (
                                        transfer_progress[temp_path].get("status", TransferStatus.NEVER_STARTED)
                                        != TransferStatus.PAUSED
                                    ):
                                        transfer_progress[temp_path]["status"] = TransferStatus.DOWNLOADING

                                    # If the offset was 0 (i.e. new download) or if a progress bar was not created
                                    # then emit the receiving_new_file signal to create a progress bar
                                    if offset == 0 or progress_widgets.get(temp_path) is None:
                                        if self.parent_dir is None:
                                            self.signals.receiving_new_file.emit((temp_path, file_header["size"]))

                                    # Keep receiving file chunks until no more chunks are received
                                    # or if the transfer is paused
                                    while True:
                                        if transfer_progress[temp_path]["status"] == TransferStatus.PAUSED:
                                            # If the transfer is paused, close the file and socket
                                            file_to_write.close()
                                            file_recv_socket.close()
                                            return

                                        # Receive a file chunk
                                        file_bytes_read: bytes = sender.recv(FILE_BUFFER_LEN)

                                        # Compute the hash if it is a new download and if it is not a small file
                                        if not offset and not is_tiny_file:
                                            hash.update(file_bytes_read)

                                        num_bytes_read = len(file_bytes_read)
                                        byte_count += num_bytes_read

                                        # Update the file progress
                                        transfer_progress[temp_path]["progress"] = byte_count + offset

                                        file_to_write.write(file_bytes_read)

                                        # If it is a file download, emit the file_progress_update signal
                                        if self.parent_dir is None:
                                            self.signals.file_progress_update.emit(temp_path)

                                        # Otherwise, emit the dir_progress_update signal
                                        else:
                                            self.signals.dir_progress_update.emit((self.parent_dir, num_bytes_read))
                                        if num_bytes_read == 0:
                                            break

                                    hash_str = ""
                                    # If this download was resumed, compute the hash from the start of the file
                                    if offset:
                                        file_to_write.seek(0)
                                        hash_str = get_file_hash(str(temp_path))
                                    file_to_write.close()

                                    # Check if the hashes received from the server (or sender) match
                                    received_hash = hash.hexdigest() if not offset else hash_str
                                    if (
                                        is_tiny_file
                                        or (request_hash and received_hash == file_header["hash"])
                                        or (received_hash == self.file_item["hash"])
                                    ):
                                        # Change the status of this download to COMPLETED
                                        transfer_progress[temp_path]["status"] = TransferStatus.COMPLETED

                                        # Move the file into the user's download folder
                                        final_download_path.parent.mkdir(parents=True, exist_ok=True)
                                        shutil.move(temp_path, final_download_path)
                                        print("Succesfully received 1 file")

                                        # Emit the file_download_complete signal to delete the progress bar
                                        self.signals.file_download_complete.emit(temp_path)
                                        del transfer_progress[temp_path]
                                    else:
                                        transfer_progress[temp_path]["status"] = TransferStatus.FAILED
                                        logging.error(msg=f"Failed integrity check for file {file_header['path']}")
                                        show_error_dialog(
                                            f"Failed integrity check for\
                                            file {file_header['path']}.\nTry downloading it again."
                                        )
                                except Exception as e:
                                    logging.exception(e)
                                    show_error_dialog(f"File received but failed to save.\n{e}")
                            except Exception as e:
                                logging.exception(e)
                                show_error_dialog("Unable to write file.\n{e}")
                        else:
                            logging.error(
                                msg=f"Not enough space to receive file {file_header['path']}, {file_header['size']}"
                            )
                            show_error_dialog(
                                f"Insufficient storage. You need at\
                                least {convert_size(file_header['size'])} of space to receive {file_header['path']}.",
                                True,
                            )
                    else:
                        raise RequestException(
                            f"Sender sent invalid message type in header: {res_type}",
                            ExceptionCode.INVALID_HEADER,
                        )
                case HeaderCode.ERROR.value:
                    # Receive the exception from the sender and raise it
                    res_len = int(self.client_peer_socket.recv(HEADER_MSG_LEN).decode(FMT).strip())
                    res = self.client_peer_socket.recv(res_len)
                    err: RequestException = msgpack.unpackb(
                        res,
                        object_hook=RequestException.from_dict,
                        raw=False,
                    )
                    raise err
                case _:
                    err = RequestException(
                        f"Invalid message type in header: {res_type}",
                        ExceptionCode.INVALID_HEADER,
                    )
                    raise err
        except Exception as e:
            logging.exception(e)
            show_error_dialog(f"Error occurred when requesting file.\n{e}")
        finally:
            # Close the socket
            self.client_peer_socket.close()


class Ui_FluxMainWindow(QWidget):
    """A worker that requests files or directories to be downloaded from the selected peer
    Attributes
    ----------
    MainWindow : MainWindow
        The application's main window instance
    Methods
    ----------
    dump_progress_data()
        Saves the progress data to disk when the application is closed
    send_message()
        Sends a message to the selected user
    closeEvent(event) (Overriden)
        Closes the heartbeat thread before exiting the application
    render_file_tree(share: list[DirData] | None, parent: QTreeWidgetItem)
        Populates the file tree widget with data of the share data file tree of the selected user
    on_file_item_selected()
        Updates the information label and download button with the selected file items
    download_files()
        Starts threads to download the selected files
    messages_controller(message: Message)
        Updates the message area with a new message
    render_messages(messages_list: list[Message])
        Renders all the messages in the message area
    update_online_status(new_status: dict[str, int])
        Updates the last seen statuses of users
    on_user_selection_changed()
        Updates the file tree and message area with the data of the new selected user
    setupUi(MainWindow: MainWindow)
        Sets up the initial UI, performs application initialization steps
    retranslateUi(MainWindow: MainWindow)
        Sets text on labels and buttons, sets options for components
    open_settings(MainWindow: MainWindow)
        Opens up the settings dialog
    open_file_info(MainWindow: MainWindow)
        Opens the file info dialog
    import_files()
        Opens the file picker to import files into the share folder
    import_folder()
        Opens the file picker to import folders into the share folder
    share_file()
        Opens the file picker to select files to send via Direct Transfer
    pause_download(path: Path)
        Pauses the download of a file
    resume_download(path: Path)
        Resumes the download of a file
    remove_progress_widget(path: Path)
        Remove the progress bar corresponding to a file download
    new_file_progress(data: tuple[Path, int])
        Create a new progress bar for a file download
    update_file_progress(path: Path)
        Updates the progress bar with the latest progress of the file download
    update_dir_progress(progress_data: tuple[Path, int])
        Updates the progress bar with the latest progress of the directory download
    direct_transfer_controller(data: tuple[FileMetadata, socket.socket])
        Opens a dialog asking the user to accept or reject an incoming Direct Transfer request
    direct_transfer_accept(metadata: FileMetadata, sender: str, peer_socket: socket.socket)
        Accepts the Direct Transfer request and opens a new socket to receive the file
    direct_transfer_reject(peer_socket: socket.socket)
        Rejects the Direct Transfer request
    open_global_search(MainWindow: MainWindow)
        Opens the file search dialog
    download_from_global_search(item: ItemSearchResult)
        Downloads the selected item from the search dialog
    """

    global client_send_socket
    global client_recv_socket
    global uname_to_status

    def __init__(self, MainWindow: MainWindow):
        self.MainWindow = MainWindow
        super(Ui_FluxMainWindow, self).__init__()
        try:
            global user_settings
            global dir_progress
            global transfer_progress
            global progress_widgets

            self.user_settings = MainWindow.user_settings
            self.signals = Signals()

            # Connect signals
            self.signals.pause_download.connect(self.pause_download)
            self.signals.resume_download.connect(self.resume_download)

            user_settings = MainWindow.user_settings
            try:
                # Load pickled transfer progress if it exists
                with (Path.home() / ".Flux/db/transfer_progress.pkl").open(mode="rb") as transfer_progress_dump:
                    transfer_progress_dump.seek(0)
                    transfer_progress = pickle.load(transfer_progress_dump)
            except Exception as e:
                # Generate the transfer progress if no pickle was created
                logging.error(msg=f"Failed to load transfer progress from dump: {e}")
                transfer_progress = generate_transfer_progress()
                logging.debug(msg=f"Transfer Progress generated\n{pformat(transfer_progress)}")
            try:
                # Load pickled dir_progress if it exists
                with (Path.home() / ".Flux/db/dir_progress.pkl").open(mode="rb") as dir_progress_dump:
                    dir_progress_dump.seek(0)
                    dir_progress_readable: dict[Path, DirProgress] = pickle.load(dir_progress_dump)
                    for path, data in dir_progress_readable.items():
                        dir_progress[path] = data
                        dir_progress[path]["mutex"] = QMutex()
                    logging.debug(msg=f"Dir progress loaded from dump\n{pformat(dir_progress)}")
            except Exception as e:
                # TODO: Generate transfer progress for directories
                logging.error(msg=f"Failed to load dir progress from dump: {e}")

            # Connect to the server given in the settings
            SERVER_IP = self.user_settings["server_ip"]
            SERVER_ADDR = (SERVER_IP, SERVER_RECV_PORT)
            client_send_socket.settimeout(10)
            client_send_socket.connect(SERVER_ADDR)
            client_send_socket.settimeout(None)

            # Attempt to register the chosen username
            self_uname = self.user_settings["uname"]
            username = self_uname.encode(FMT)
            username_header = f"{HeaderCode.NEW_CONNECTION.value}{len(username):<{HEADER_MSG_LEN}}".encode(FMT)
            client_send_socket.send(username_header + username)
            type = client_send_socket.recv(HEADER_TYPE_LEN).decode(FMT)
            if type != HeaderCode.NEW_CONNECTION.value:
                # If the registration fails, receive the error
                error_len = int(client_send_socket.recv(HEADER_MSG_LEN).decode(FMT).strip())
                error = client_send_socket.recv(error_len)
                exception: RequestException = msgpack.unpackb(error, object_hook=RequestException.from_dict, raw=False)
                if exception.code == ExceptionCode.USER_EXISTS:
                    logging.error(msg=exception.msg)
                    show_error_dialog("Sorry that username is taken, please choose another one", True)
                else:
                    logging.fatal(msg=exception.msg)
                    print("\nSorry something went wrong")
                    client_send_socket.close()
                    client_recv_socket.close()
                    MainWindow.close()

            # Send share data to the server
            update_share_data(Path(self.user_settings["share_folder_path"]), client_send_socket)

            # Spawn heartbeat worker in its own thread
            self.heartbeat_thread = QThread()
            self.heartbeat_worker = HeartbeatWorker()
            self.heartbeat_worker.moveToThread(self.heartbeat_thread)
            self.heartbeat_thread.started.connect(self.heartbeat_worker.run)
            self.heartbeat_worker.update_status.connect(self.update_online_status)
            self.heartbeat_thread.start()

            # self.save_progress_thread = QThread()
            # self.save_progress_worker = SaveProgressWorker()
            # self.save_progress_worker.moveToThread(self.save_progress_thread)
            # self.save_progress_thread.started.connect(self.save_progress_worker.run)
            # self.save_progress_thread.start()

            self.receive_thread = QThread()
            self.receive_worker = ReceiveHandler()
            self.receive_worker.moveToThread(self.receive_thread)
            self.receive_thread.started.connect(self.receive_worker.run)
            self.receive_worker.message_received.connect(self.messages_controller)
            self.receive_worker.file_incoming.connect(self.direct_transfer_controller)
            self.receive_thread.start()

        except Exception as e:
            logging.error(f"Could not connect to server: {e}")
            sys.exit(
                show_error_dialog(
                    f"Could not connect to server:\
                    {e}\nEnsure that the server is online and you have entered the correct server IP.",
                    True,
                )
            )

        self.setupUi(MainWindow)

        try:
            with (Path.home() / ".Flux/db/progress_widgets.pkl").open(mode="rb") as progress_widgets_dump:
                progress_widgets_dump.seek(0)
                progress_widgets_readable: dict[Path, ProgressBarData] = pickle.load(progress_widgets_dump)
                for path, data in progress_widgets_readable.items():
                    self.new_file_progress((path, data["total"]))
                    progress_widgets[path].ui.update_progress(data["current"])
                    progress_widgets[path].ui.btn_Toggle.setText("▶")
                    progress_widgets[path].ui.paused = True
                logging.debug(msg=f"Progress widgets loaded from dump\n{pformat(progress_widgets)}")
        except Exception as e:
            # Fallback if no dump was created
            logging.error(msg=f"Failed to load progress widgets from dump: {e}")
            # TODO: Generate transfer progress for progress widgets

    def dump_progress_data(self) -> None:
        """A method used to save progress data to disk. Called externally by the close event of MainWindow."""

        worker = SaveProgressWorker()
        worker.dump_progress_data()

    def send_message(self) -> None:
        """Sends a message to the global selected user"""

        global client_send_socket
        global client_recv_socket
        global messages_store
        global server_socket_mutex
        global selected_uname

        if self.txtedit_MessageInput.toPlainText() == "":
            return

        # Acquire lock for server socket
        server_socket_mutex.lock()
        peer_ip = ""
        # Request peer ip from server if not cached
        if uname_to_ip.get(selected_uname) is None:
            peer_ip = request_ip(selected_uname, client_send_socket)
            uname_to_ip[selected_uname] = peer_ip  # Update cache
        # Use cached peer ip
        else:
            peer_ip = uname_to_ip.get(selected_uname)
        # Release server socket
        server_socket_mutex.unlock()
        if peer_ip is not None:
            # Send message to peer
            client_peer_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_peer_socket.connect((peer_ip, CLIENT_RECV_PORT))
            msg = self.txtedit_MessageInput.toPlainText()
            msg_bytes = msg.encode(FMT)
            header = f"{HeaderCode.MESSAGE.value}{len(msg_bytes):<{HEADER_MSG_LEN}}".encode(FMT)
            try:
                client_peer_socket.send(header + msg_bytes)
                # Update local messages store
                if messages_store.get(selected_uname) is not None:
                    messages_store[selected_uname].append({"sender": self_uname, "content": msg})
                else:
                    messages_store[selected_uname] = [{"sender": self_uname, "content": msg}]
                self.render_messages(messages_store[selected_uname])
            except Exception as e:
                logging.error(f"Failed to send message: {e}")
                show_error_dialog(f"Failed to send message.\n{e}")
            finally:
                self.txtedit_MessageInput.clear()
        else:
            logging.error(f"Could not find ip for user {selected_uname}")
            show_error_dialog(f"This user has gone offline or does not exist. Try again later")

    def closeEvent(self, event) -> None:
        """Closes the heartbeat thread before exiting the application"""

        self.heartbeat_thread.exit()
        return super().closeEvent(event)

    def render_file_tree(self, share: list[DirData] | None, parent: QTreeWidgetItem) -> None:
        """Recursively traverse a directory structure to render in a tree widget
        Parameters
        ----------
        share : list[DirData] | None
            Dictionary representing a directory structure. Holds None for leaf nodes (files).
        parent : QTreeWidgetItem
            Parent item widget in the rendered tree
        """

        if share is None:
            return

        # Create widget for each item in the current level of the file tree
        for item in share:
            if item["type"] == "file":
                file_item = QTreeWidgetItem(parent)
                file_item.setText(0, item["name"])
                file_item.setData(0, Qt.UserRole, item)
            else:
                dir_item = QTreeWidgetItem(parent)
                dir_item.setText(0, item["name"] + "/")
                dir_item.setData(0, Qt.UserRole, item)
                # Recursive call for immediate children
                self.render_file_tree(item["children"], dir_item)

    def on_file_item_selected(self) -> None:
        """Slot to perform actions when user selects a file.
        This method sets the value of the global selected_file_items object. It also selectively enables or disables the File Info button.
        """

        global selected_file_items

        selected_items = self.file_tree.selectedItems()
        selected_file_items = []
        # Create list of file items
        for item in selected_items:
            data: DirData = item.data(0, Qt.UserRole)
            selected_file_items.append(data)
        # Enable info btn if 1 item is selected
        if len(selected_items) == 1:
            self.lbl_FileInfo.setText(selected_file_items[0]["name"])
            self.btn_FileInfo.setEnabled(True)
        # Disable info btn if 0 or multiple items are selected
        else:
            self.lbl_FileInfo.setText(f"{len(selected_items)} items selected")
            self.btn_FileInfo.setEnabled(False)

    def download_files(self) -> None:
        """Method to start downloads for the global selected file items."""

        global selected_uname
        global client_send_socket
        global transfer_progress
        global selected_file_items
        global server_socket_mutex
        global uname_to_ip

        # Thread pool instanced
        request_file_pool = QThreadPool.globalInstance()

        for selected_item in selected_file_items:
            server_socket_mutex.lock()
            peer_ip = ""
            # Use cache to obtain peer ip
            if uname_to_ip.get(selected_uname) is None:
                peer_ip = request_ip(selected_uname, client_send_socket)
                uname_to_ip[selected_uname] = peer_ip
            else:
                peer_ip = uname_to_ip.get(selected_uname)
            server_socket_mutex.unlock()
            if peer_ip is None:
                logging.error(f"Selected user {selected_uname} does not exist")
                show_error_dialog(f"Selected user {selected_uname} does not exist")
                return
            # New transfer progress entry added
            transfer_progress[TEMP_FOLDER_PATH / selected_uname / selected_item["path"]] = {
                "progress": 0,
                "status": TransferStatus.NEVER_STARTED,
            }
            # Start file download thread in pool
            if selected_item["type"] == "file":
                request_file_worker = RequestFileWorker(selected_item, peer_ip, selected_uname, None)
                request_file_worker.signals.receiving_new_file.connect(self.new_file_progress)
                request_file_worker.signals.file_progress_update.connect(self.update_file_progress)
                request_file_worker.signals.file_download_complete.connect(self.remove_progress_widget)
                request_file_pool.start(request_file_worker)
            # Start folder download threads in pool
            else:
                files_to_request: list[DirData] = []
                get_files_in_dir(
                    selected_item["children"],
                    files_to_request,
                )
                dir_path = TEMP_FOLDER_PATH / selected_uname / selected_item["path"]
                # New directory progress entry added
                dir_progress[dir_path] = {
                    "current": 0,
                    "total": get_directory_size(selected_item, 0, 0)[0],
                    "status": TransferStatus.DOWNLOADING,
                    "mutex": QMutex(),
                }
                self.new_file_progress((dir_path, dir_progress[dir_path]["total"]))
                # Add transfer progress for all files in folder
                for f in files_to_request:
                    transfer_progress[TEMP_FOLDER_PATH / selected_uname / f["path"]] = {
                        "progress": 0,
                        "status": TransferStatus.NEVER_STARTED,
                    }
                # Start threads for all files in folder
                for file in files_to_request:
                    request_file_worker = RequestFileWorker(file, peer_ip, selected_uname, dir_path)
                    request_file_worker.signals.dir_progress_update.connect(self.update_dir_progress)
                    request_file_pool.start(request_file_worker)

    def messages_controller(self, message: Message) -> None:
        """Method to conditionally render chat messages.
        Only performs the render operation if the received message is from the actively selected user.
        Parameters
        ----------
        message : Message
            the latest received message object
        """

        global selected_uname
        global self_uname
        # Only start rendering if selected user is sender
        if message["sender"] == selected_uname:
            self.render_messages(messages_store[selected_uname])

    def render_messages(self, messages_list: list[Message]) -> None:
        """Performs the render operation for chat messages.
        Clears message area and replaces it with new html for the selected user's message history. Automatically scrolls down widget to new content.
        Parameters
        ----------
        messages_list : list[Message]
            list of message objects to be displayed, in order.
        """

        global self_uname
        if messages_list is None or messages_list == []:
            self.txtedit_MessagesArea.clear()
            return
        # Construct message area html
        messages_html = LEADING_HTML
        for message in messages_list:
            messages_html += construct_message_html(message, message["sender"] == self_uname)
        messages_html += TRAILING_HTML
        self.txtedit_MessagesArea.setHtml(messages_html)
        # Scroll to latest
        self.txtedit_MessagesArea.verticalScrollBar().setValue(self.txtedit_MessagesArea.verticalScrollBar().maximum())

    def update_online_status(self, new_status: dict[str, int]) -> None:
        """Slot function that updates status display for users on the network.
        Called by the update_status signal.
        Parameters
        ----------
        new_status : dict[str, int]
            latest fetched status dictionary that maps username to a last active timestamp.
        """

        global uname_to_status
        # Existing users in local list
        old_users = set(uname_to_status.keys())
        # Users not initially in local list
        new_users = set(new_status.keys())
        # List of users to add
        to_add = new_users.difference(old_users)
        # List of users to remove
        users_to_remove = old_users.difference(new_users)

        # Update or remove widgets for users already present in local list
        for index in range(self.lw_OnlineStatus.count()):
            item = self.lw_OnlineStatus.item(index)
            username = item.data(Qt.UserRole)
            if username in users_to_remove:
                item.setIcon(self.icon_Offline)
                timestamp = time.localtime(uname_to_status[username])
                item.setText(username + (f" (last active: {time.strftime('%d-%m-%Y %H:%M:%S', timestamp)})"))
            else:
                item.setIcon(
                    self.icon_Online if time.time() - new_status[username] <= ONLINE_TIMEOUT else self.icon_Offline
                )
                timestamp = time.localtime(new_status[username])
                item.setText(
                    username
                    + (
                        ""
                        if time.time() - new_status[username] <= ONLINE_TIMEOUT
                        else f" (last active: {time.strftime('%d-%m-%Y %H:%M:%S', timestamp)})"
                    )
                )
        # Add widgets for new users
        for uname in to_add:
            status_item = QListWidgetItem(self.lw_OnlineStatus)
            status_item.setIcon(
                self.icon_Online if time.time() - new_status[uname] <= ONLINE_TIMEOUT else self.icon_Offline
            )
            timestamp = time.localtime(new_status[uname])
            status_item.setData(Qt.UserRole, uname)
            status_item.setText(
                uname + ""
                if time.time() - new_status[uname] <= ONLINE_TIMEOUT
                else f" (last active: {time.strftime('%d-%m-%Y %H:%M:%S', timestamp)})"
            )
        uname_to_status = new_status



class Ui_FluxMainWindow(object):
    def setupUi(self, MainWindow):
        if not MainWindow.objectName():
            MainWindow.setObjectName("MainWindow")
        MainWindow.resize(881, 744)
        sizePolicy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(MainWindow.sizePolicy().hasHeightForWidth())
        MainWindow.setSizePolicy(sizePolicy)
        MainWindow.setUnifiedTitleAndToolBarOnMac(False)
        self.centralwidget = QWidget(MainWindow)
        self.centralwidget.setObjectName("centralwidget")
        self.verticalLayout_4 = QVBoxLayout(self.centralwidget)
        self.verticalLayout_4.setObjectName("verticalLayout_4")
        self.verticalLayout = QVBoxLayout()
        self.verticalLayout.setObjectName("verticalLayout")
        self.verticalLayout.setSizeConstraint(QLayout.SetMaximumSize)
        self.verticalLayout.setContentsMargins(10, 0, 10, -1)
        self.Buttons = QHBoxLayout()
        self.Buttons.setSpacing(6)
        self.Buttons.setObjectName("Buttons")
        self.Buttons.setSizeConstraint(QLayout.SetDefaultConstraint)
        self.label_3 = QLabel(self.centralwidget)
        self.label_3.setObjectName("label_3")
        sizePolicy1 = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sizePolicy1.setHorizontalStretch(0)
        sizePolicy1.setVerticalStretch(0)
        sizePolicy1.setHeightForWidth(self.label_3.sizePolicy().hasHeightForWidth())
        self.label_3.setSizePolicy(sizePolicy1)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        font.setWeight(75)
        self.label_3.setFont(font)
        self.label_3.setMargin(0)

        self.Buttons.addWidget(self.label_3)

        self.horizontalSpacer = QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.Buttons.addItem(self.horizontalSpacer)

        self.pushButton_2 = QPushButton(self.centralwidget)
        self.pushButton_2.setObjectName("pushButton_2")

        self.Buttons.addWidget(self.pushButton_2)

        self.pushButton = QPushButton(self.centralwidget)
        self.pushButton.setObjectName("pushButton")

        self.Buttons.addWidget(self.pushButton)

        self.verticalLayout.addLayout(self.Buttons)

        self.Content = QHBoxLayout()
        self.Content.setObjectName("Content")
        self.Files = QVBoxLayout()
        self.Files.setObjectName("Files")
        self.label = QLabel(self.centralwidget)
        self.label.setObjectName("label")

        self.Files.addWidget(self.label)

        self.treeWidget = QTreeWidget(self.centralwidget)
        QTreeWidgetItem(self.treeWidget)
        __qtreewidgetitem = QTreeWidgetItem(self.treeWidget)
        QTreeWidgetItem(__qtreewidgetitem)
        QTreeWidgetItem(__qtreewidgetitem)
        QTreeWidgetItem(__qtreewidgetitem)
        QTreeWidgetItem(self.treeWidget)
        __qtreewidgetitem1 = QTreeWidgetItem(self.treeWidget)
        QTreeWidgetItem(__qtreewidgetitem1)
        QTreeWidgetItem(__qtreewidgetitem1)
        QTreeWidgetItem(__qtreewidgetitem1)
        QTreeWidgetItem(__qtreewidgetitem1)
        QTreeWidgetItem(self.treeWidget)
        self.treeWidget.setObjectName("treeWidget")
        self.treeWidget.header().setVisible(True)

        self.Files.addWidget(self.treeWidget)

        self.horizontalLayout_2 = QHBoxLayout()
        self.horizontalLayout_2.setObjectName("horizontalLayout_2")
        self.label_4 = QLabel(self.centralwidget)
        self.label_4.setObjectName("label_4")

        self.horizontalLayout_2.addWidget(self.label_4)

        self.horizontalSpacer_2 = QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum)

        self.horizontalLayout_2.addItem(self.horizontalSpacer_2)

        self.pushButton_5 = QPushButton(self.centralwidget)
        self.pushButton_5.setObjectName("pushButton_5")
        self.pushButton_5.setEnabled(True)

        self.horizontalLayout_2.addWidget(self.pushButton_5)

        self.pushButton_4 = QPushButton(self.centralwidget)
        self.pushButton_4.setObjectName("pushButton_4")
        self.pushButton_4.setEnabled(True)

        self.horizontalLayout_2.addWidget(self.pushButton_4)

        self.Files.addLayout(self.horizontalLayout_2)

        self.Files.setStretch(1, 2)

        self.Content.addLayout(self.Files)

        self.Users = QVBoxLayout()
        self.Users.setObjectName("Users")
        self.Users.setSizeConstraint(QLayout.SetDefaultConstraint)
        self.label_2 = QLabel(self.centralwidget)
        self.label_2.setObjectName("label_2")

        self.Users.addWidget(self.label_2)

        self.listWidget = QListWidget(self.centralwidget)
        icon = QIcon()
        icon.addFile("ui/res/earth.png", QSize(), QIcon.Normal, QIcon.Off)
        __qlistwidgetitem = QListWidgetItem(self.listWidget)
        __qlistwidgetitem.setIcon(icon)
        __qlistwidgetitem1 = QListWidgetItem(self.listWidget)
        __qlistwidgetitem1.setIcon(icon)
        icon1 = QIcon()
        icon1.addFile("ui/res/web-off.png", QSize(), QIcon.Normal, QIcon.Off)
        __qlistwidgetitem2 = QListWidgetItem(self.listWidget)
        __qlistwidgetitem2.setIcon(icon1)
        __qlistwidgetitem3 = QListWidgetItem(self.listWidget)
        __qlistwidgetitem3.setIcon(icon)
        self.listWidget.setObjectName("listWidget")
        self.listWidget.setSortingEnabled(False)

        self.Users.addWidget(self.listWidget)

        self.textEdit = QTextEdit(self.centralwidget)
        self.textEdit.setObjectName("textEdit")
        sizePolicy2 = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        sizePolicy2.setHorizontalStretch(0)
        sizePolicy2.setVerticalStretch(5)
        sizePolicy2.setHeightForWidth(self.textEdit.sizePolicy().hasHeightForWidth())
        self.textEdit.setSizePolicy(sizePolicy2)
        self.textEdit.setMinimumSize(QSize(0, 0))
        self.textEdit.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.Users.addWidget(self.textEdit)

        self.horizontalLayout = QHBoxLayout()
        self.horizontalLayout.setObjectName("horizontalLayout")
        self.horizontalLayout.setSizeConstraint(QLayout.SetDefaultConstraint)
        self.plainTextEdit = QPlainTextEdit(self.centralwidget)
        self.plainTextEdit.setObjectName("plainTextEdit")
        self.plainTextEdit.setEnabled(True)
        sizePolicy3 = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        sizePolicy3.setHorizontalStretch(0)
        sizePolicy3.setVerticalStretch(0)
        sizePolicy3.setHeightForWidth(self.plainTextEdit.sizePolicy().hasHeightForWidth())
        self.plainTextEdit.setSizePolicy(sizePolicy3)
        self.plainTextEdit.setMaximumSize(QSize(16777215, 80))

        self.horizontalLayout.addWidget(self.plainTextEdit)

        self.verticalLayout_6 = QVBoxLayout()
        self.verticalLayout_6.setObjectName("verticalLayout_6")
        self.pushButton_3 = QPushButton(self.centralwidget)
        self.pushButton_3.setObjectName("pushButton_3")
        sizePolicy4 = QSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)
        sizePolicy4.setHorizontalStretch(0)
        sizePolicy4.setVerticalStretch(0)
        sizePolicy4.setHeightForWidth(self.pushButton_3.sizePolicy().hasHeightForWidth())
        self.pushButton_3.setSizePolicy(sizePolicy4)

        self.verticalLayout_6.addWidget(self.pushButton_3)

        self.pushButton_6 = QPushButton(self.centralwidget)
        self.pushButton_6.setObjectName("pushButton_6")
        sizePolicy4.setHeightForWidth(self.pushButton_6.sizePolicy().hasHeightForWidth())
        self.pushButton_6.setSizePolicy(sizePolicy4)

        self.verticalLayout_6.addWidget(self.pushButton_6)

        self.horizontalLayout.addLayout(self.verticalLayout_6)

        self.Users.addLayout(self.horizontalLayout)

        self.Users.setStretch(1, 4)
        self.Users.setStretch(3, 1)

        self.Content.addLayout(self.Users)

        self.Content.setStretch(0, 3)
        self.Content.setStretch(1, 2)

        self.verticalLayout.addLayout(self.Content)

        self.label_12 = QLabel(self.centralwidget)
        self.label_12.setObjectName("label_12")

        self.verticalLayout.addWidget(self.label_12)

        self.scrollArea = QScrollArea(self.centralwidget)
        self.scrollArea.setObjectName("scrollArea")
        sizePolicy5 = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sizePolicy5.setHorizontalStretch(0)
        sizePolicy5.setVerticalStretch(0)
        sizePolicy5.setHeightForWidth(self.scrollArea.sizePolicy().hasHeightForWidth())
        self.scrollArea.setSizePolicy(sizePolicy5)
        self.scrollArea.setMaximumSize(QSize(16777215, 150))
        self.scrollArea.setWidgetResizable(True)
        self.scrollAreaWidgetContents = QWidget()
        self.scrollAreaWidgetContents.setObjectName("scrollAreaWidgetContents")
        self.scrollAreaWidgetContents.setGeometry(QRect(0, 0, 831, 328))
        self.verticalLayout_5 = QVBoxLayout(self.scrollAreaWidgetContents)
        self.verticalLayout_5.setObjectName("verticalLayout_5")
        self.widget_6 = QWidget(self.scrollAreaWidgetContents)
        self.widget_6.setObjectName("widget_6")
        sizePolicy6 = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        sizePolicy6.setHorizontalStretch(0)
        sizePolicy6.setVerticalStretch(0)
        sizePolicy6.setHeightForWidth(self.widget_6.sizePolicy().hasHeightForWidth())
        self.widget_6.setSizePolicy(sizePolicy6)
        self.widget_6.setMinimumSize(QSize(0, 40))
        self.widget_6.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_8 = QHBoxLayout(self.widget_6)
        self.horizontalLayout_8.setObjectName("horizontalLayout_8")
        self.label_10 = QLabel(self.widget_6)
        self.label_10.setObjectName("label_10")

        self.horizontalLayout_8.addWidget(self.label_10)

        self.progressBar_6 = QProgressBar(self.widget_6)
        self.progressBar_6.setObjectName("progressBar_6")
        self.progressBar_6.setValue(24)

        self.horizontalLayout_8.addWidget(self.progressBar_6)

        self.verticalLayout_5.addWidget(self.widget_6)

        self.widget_3 = QWidget(self.scrollAreaWidgetContents)
        self.widget_3.setObjectName("widget_3")
        sizePolicy6.setHeightForWidth(self.widget_3.sizePolicy().hasHeightForWidth())
        self.widget_3.setSizePolicy(sizePolicy6)
        self.widget_3.setMinimumSize(QSize(0, 40))
        self.widget_3.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_5 = QHBoxLayout(self.widget_3)
        self.horizontalLayout_5.setObjectName("horizontalLayout_5")
        self.label_7 = QLabel(self.widget_3)
        self.label_7.setObjectName("label_7")

        self.horizontalLayout_5.addWidget(self.label_7)

        self.progressBar_3 = QProgressBar(self.widget_3)
        self.progressBar_3.setObjectName("progressBar_3")
        self.progressBar_3.setValue(24)

        self.horizontalLayout_5.addWidget(self.progressBar_3)

        self.verticalLayout_5.addWidget(self.widget_3)

        self.widget_4 = QWidget(self.scrollAreaWidgetContents)
        self.widget_4.setObjectName("widget_4")
        sizePolicy6.setHeightForWidth(self.widget_4.sizePolicy().hasHeightForWidth())
        self.widget_4.setSizePolicy(sizePolicy6)
        self.widget_4.setMinimumSize(QSize(0, 40))
        self.widget_4.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_6 = QHBoxLayout(self.widget_4)
        self.horizontalLayout_6.setObjectName("horizontalLayout_6")
        self.label_8 = QLabel(self.widget_4)
        self.label_8.setObjectName("label_8")

        self.horizontalLayout_6.addWidget(self.label_8)

        self.progressBar_4 = QProgressBar(self.widget_4)
        self.progressBar_4.setObjectName("progressBar_4")
        self.progressBar_4.setValue(24)

        self.horizontalLayout_6.addWidget(self.progressBar_4)

        self.verticalLayout_5.addWidget(self.widget_4)

        self.widget = QWidget(self.scrollAreaWidgetContents)
        self.widget.setObjectName("widget")
        sizePolicy6.setHeightForWidth(self.widget.sizePolicy().hasHeightForWidth())
        self.widget.setSizePolicy(sizePolicy6)
        self.widget.setMinimumSize(QSize(0, 40))
        self.widget.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_3 = QHBoxLayout(self.widget)
        self.horizontalLayout_3.setObjectName("horizontalLayout_3")
        self.label_5 = QLabel(self.widget)
        self.label_5.setObjectName("label_5")

        self.horizontalLayout_3.addWidget(self.label_5)

        self.progressBar = QProgressBar(self.widget)
        self.progressBar.setObjectName("progressBar")
        self.progressBar.setValue(24)

        self.horizontalLayout_3.addWidget(self.progressBar)

        self.verticalLayout_5.addWidget(self.widget, 0, Qt.AlignTop)

        self.widget_5 = QWidget(self.scrollAreaWidgetContents)
        self.widget_5.setObjectName("widget_5")
        sizePolicy6.setHeightForWidth(self.widget_5.sizePolicy().hasHeightForWidth())
        self.widget_5.setSizePolicy(sizePolicy6)
        self.widget_5.setMinimumSize(QSize(0, 40))
        self.widget_5.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_7 = QHBoxLayout(self.widget_5)
        self.horizontalLayout_7.setObjectName("horizontalLayout_7")
        self.label_9 = QLabel(self.widget_5)
        self.label_9.setObjectName("label_9")

        self.horizontalLayout_7.addWidget(self.label_9)

        self.progressBar_5 = QProgressBar(self.widget_5)
        self.progressBar_5.setObjectName("progressBar_5")
        self.progressBar_5.setValue(24)

        self.horizontalLayout_7.addWidget(self.progressBar_5)

        self.verticalLayout_5.addWidget(self.widget_5)

        self.widget_2 = QWidget(self.scrollAreaWidgetContents)
        self.widget_2.setObjectName("widget_2")
        sizePolicy6.setHeightForWidth(self.widget_2.sizePolicy().hasHeightForWidth())
        self.widget_2.setSizePolicy(sizePolicy6)
        self.widget_2.setMinimumSize(QSize(0, 40))
        self.widget_2.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_4 = QHBoxLayout(self.widget_2)
        self.horizontalLayout_4.setObjectName("horizontalLayout_4")
        self.label_6 = QLabel(self.widget_2)
        self.label_6.setObjectName("label_6")

        self.horizontalLayout_4.addWidget(self.label_6)

        self.progressBar_2 = QProgressBar(self.widget_2)
        self.progressBar_2.setObjectName("progressBar_2")
        self.progressBar_2.setValue(24)

        self.horizontalLayout_4.addWidget(self.progressBar_2)

        self.verticalLayout_5.addWidget(self.widget_2)

        self.widget_7 = QWidget(self.scrollAreaWidgetContents)
        self.widget_7.setObjectName("widget_7")
        sizePolicy6.setHeightForWidth(self.widget_7.sizePolicy().hasHeightForWidth())
        self.widget_7.setSizePolicy(sizePolicy6)
        self.widget_7.setMinimumSize(QSize(0, 40))
        self.widget_7.setMaximumSize(QSize(16777215, 40))
        self.horizontalLayout_9 = QHBoxLayout(self.widget_7)
        self.horizontalLayout_9.setObjectName("horizontalLayout_9")
        self.label_11 = QLabel(self.widget_7)
        self.label_11.setObjectName("label_11")

        self.horizontalLayout_9.addWidget(self.label_11)

        self.progressBar_7 = QProgressBar(self.widget_7)
        self.progressBar_7.setObjectName("progressBar_7")
        self.progressBar_7.setValue(24)

        self.horizontalLayout_9.addWidget(self.progressBar_7)

        self.verticalLayout_5.addWidget(self.widget_7, 0, Qt.AlignTop)

        self.scrollArea.setWidget(self.scrollAreaWidgetContents)

        self.verticalLayout.addWidget(self.scrollArea)

        self.verticalLayout_4.addLayout(self.verticalLayout)

        MainWindow.setCentralWidget(self.centralwidget)

        self.retranslateUi(MainWindow)

        QMetaObject.connectSlotsByName(MainWindow)

    # setupUi

    def retranslateUi(self, MainWindow):
        MainWindow.setWindowTitle(QCoreApplication.translate("MainWindow", "Flux", None))
        self.label_3.setText(QCoreApplication.translate("MainWindow", "Flux / john_doe_", None))
        self.pushButton_2.setText(QCoreApplication.translate("MainWindow", "Add Files", None))
        self.pushButton.setText(QCoreApplication.translate("MainWindow", "Settings", None))
        self.label.setText(QCoreApplication.translate("MainWindow", "Browse Files", None))
        ___qtreewidgetitem = self.treeWidget.headerItem()
        ___qtreewidgetitem.setText(
            0, QCoreApplication.translate("MainWindow", "RichardRoe12", None)
        )

        __sortingEnabled = self.treeWidget.isSortingEnabled()
        self.treeWidget.setSortingEnabled(False)
        ___qtreewidgetitem1 = self.treeWidget.topLevelItem(0)
        ___qtreewidgetitem1.setText(
            0, QCoreApplication.translate("MainWindow", "photoshop.iso", None)
        )
        ___qtreewidgetitem2 = self.treeWidget.topLevelItem(1)
        ___qtreewidgetitem2.setText(0, QCoreApplication.translate("MainWindow", "Movies/", None))
        ___qtreewidgetitem3 = ___qtreewidgetitem2.child(0)
        ___qtreewidgetitem3.setText(
            0, QCoreApplication.translate("MainWindow", "The Matrix.mov", None)
        )
        ___qtreewidgetitem4 = ___qtreewidgetitem2.child(1)
        ___qtreewidgetitem4.setText(
            0, QCoreApplication.translate("MainWindow", "Forrest Gump.mp4", None)
        )
        ___qtreewidgetitem5 = ___qtreewidgetitem2.child(2)
        ___qtreewidgetitem5.setText(0, QCoreApplication.translate("MainWindow", "Django.mp4", None))
        ___qtreewidgetitem6 = self.treeWidget.topLevelItem(2)
        ___qtreewidgetitem6.setText(
            0, QCoreApplication.translate("MainWindow", "msoffice.zip", None)
        )
        ___qtreewidgetitem7 = self.treeWidget.topLevelItem(3)
        ___qtreewidgetitem7.setText(0, QCoreApplication.translate("MainWindow", "Games/", None))
        ___qtreewidgetitem8 = ___qtreewidgetitem7.child(0)
        ___qtreewidgetitem8.setText(0, QCoreApplication.translate("MainWindow", "NFS/", None))
        ___qtreewidgetitem9 = ___qtreewidgetitem7.child(1)
        ___qtreewidgetitem9.setText(
            0, QCoreApplication.translate("MainWindow", "nfsmostwanted.zip", None)
        )
        ___qtreewidgetitem10 = ___qtreewidgetitem7.child(2)
        ___qtreewidgetitem10.setText(
            0, QCoreApplication.translate("MainWindow", "TLauncher.zip", None)
        )
        ___qtreewidgetitem11 = ___qtreewidgetitem7.child(3)
        ___qtreewidgetitem11.setText(0, QCoreApplication.translate("MainWindow", "GTA-V.iso", None))
        ___qtreewidgetitem12 = self.treeWidget.topLevelItem(4)
        ___qtreewidgetitem12.setText(
            0, QCoreApplication.translate("MainWindow", "Study Material/", None)
        )
        self.treeWidget.setSortingEnabled(__sortingEnabled)

        self.label_4.setText(
            QCoreApplication.translate("MainWindow", "Selected File/Folder: msoffice.zip", None)
        )
        self.pushButton_5.setText(QCoreApplication.translate("MainWindow", "Info", None))
        self.pushButton_4.setText(QCoreApplication.translate("MainWindow", "Download", None))
        self.label_2.setText(QCoreApplication.translate("MainWindow", "Users", None))

        __sortingEnabled1 = self.listWidget.isSortingEnabled()
        self.listWidget.setSortingEnabled(False)
        ___qlistwidgetitem = self.listWidget.item(0)
        ___qlistwidgetitem.setText(QCoreApplication.translate("MainWindow", "RichardRoe12", None))
        ___qlistwidgetitem1 = self.listWidget.item(1)
        ___qlistwidgetitem1.setText(QCoreApplication.translate("MainWindow", "ronaldw", None))
        ___qlistwidgetitem2 = self.listWidget.item(2)
        ___qlistwidgetitem2.setText(
            QCoreApplication.translate("MainWindow", "harrypotter (last active: 11:45 am)", None)
        )
        ___qlistwidgetitem3 = self.listWidget.item(3)
        ___qlistwidgetitem3.setText(QCoreApplication.translate("MainWindow", "anonymous_lol", None))
        self.listWidget.setSortingEnabled(__sortingEnabled1)

        self.textEdit.setHtml(
            QCoreApplication.translate(
                "MainWindow",
                '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">\n'
                '<html><head><meta name="qrichtext" content="1" /><style type="text/css">\n'
                "p, li { white-space: pre-wrap; }\n"
                "</style></head><body style=\" font-family:'Noto Sans'; font-size:10pt; font-weight:400; font-style:normal;\">\n"
                '<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"><span style=" font-weight:600; color:#e5a50a;">12:03</span><span style=" font-weight:600;"> RichardRoe12: </span>Hello</p>\n'
                '<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"><span style=" font-weight:600; color:#1a5fb4;">12:03</span><span style=" font-weight:600;"> You: </span>Hii</p>\n'
                '<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"><span style=" font-weight:600; color:#e5a50a;">12:03</span><span style="'
                ' font-weight:600;"> RichardRoe12: </span>Got any games?</p>\n'
                '<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"><span style=" font-weight:600; color:#1a5fb4;">12:03</span><span style=" font-weight:600;"> You: </span>Probably</p>\n'
                '<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"><span style=" font-weight:600; color:#1a5fb4;">12:03</span><span style=" font-weight:600;"> You: </span>Wait ill upload something...</p>',
                "</body></html>",
                # None,
            )
        )
        self.plainTextEdit.setPlaceholderText(
            QCoreApplication.translate("MainWindow", "Enter message", None)
        )
        self.pushButton_3.setText(QCoreApplication.translate("MainWindow", "Send Message", None))
        self.pushButton_6.setText(QCoreApplication.translate("MainWindow", "Send File", None))
        self.label_12.setText(QCoreApplication.translate("MainWindow", "Downloading:", None))
        self.label_10.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))
        self.label_7.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))
        self.label_8.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))
        self.label_5.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))
        self.label_9.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))
        self.label_6.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))
        self.label_11.setText(QCoreApplication.translate("MainWindow", "TextLabel", None))

    # retranslateUi
