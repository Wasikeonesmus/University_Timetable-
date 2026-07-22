import socket
import smtplib
from django.core.mail.backends.smtp import EmailBackend

class IPv4SMTPConnection(smtplib.SMTP_SSL):
    """SMTP_SSL subclass forcing IPv4 (socket.AF_INET) resolution for Docker compatibility."""
    def _get_socket(self, host, port, timeout):
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        err = None
        source_address = getattr(self, 'source_address', None)
        for res in infos:
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if timeout is not None:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                return sock
            except socket.error as e:
                err = e
                if sock is not None:
                    sock.close()
        if err is not None:
            raise err
        else:
            raise socket.error("getaddrinfo returned empty list")


class IPv4SMTPTLSConnection(smtplib.SMTP):
    """SMTP subclass forcing IPv4 (socket.AF_INET) resolution for Docker compatibility."""
    def _get_socket(self, host, port, timeout):
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        err = None
        source_address = getattr(self, 'source_address', None)
        for res in infos:
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if timeout is not None:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                return sock
            except socket.error as e:
                err = e
                if sock is not None:
                    sock.close()
        if err is not None:
            raise err
        else:
            raise socket.error("getaddrinfo returned empty list")


class IPv4EmailBackend(EmailBackend):
    """
    Custom Django EmailBackend that forces IPv4 connections.
    Prevents 'OSError: [Errno 101] Network is unreachable' caused by
    Docker container bridge networks trying unrouted IPv6 addresses.
    """
    def open(self):
        if self.connection:
            return False
        connection_class = IPv4SMTPConnection if self.use_ssl else IPv4SMTPTLSConnection
        source_addr = getattr(self, 'source_address', None)
        try:
            self.connection = connection_class(
                self.host,
                self.port,
                timeout=self.timeout,
                source_address=source_addr,
            )
            if not self.use_ssl and self.use_tls:
                self.connection.starttls()
            if self.username and self.password:
                self.connection.login(self.username, self.password)
            return True
        except Exception:
            if not self.fail_silently:
                raise
            return False
