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
