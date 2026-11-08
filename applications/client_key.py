"""Who counts as "one client" for rate limits and the daily e-mail caps.

IPv4 addresses are used as they are. An IPv6 customer normally owns a whole /64, so
counting single IPv6 addresses would let one visitor mint billions of "clients": an
IPv6 address is reduced to its /64 network. IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is
treated as the IPv4 address.
"""
import ipaddress

from flask import request


def reduce_ip(address):
    """Counting key of an IP string (unparseable input is returned unchanged)."""
    if not address:
        return address
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop a zone id (fe80::1%eth0)
    except ValueError:
        return address
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


def client_key():
    """Key function of Flask-Limiter and of the e-mail IP counter."""
    return reduce_ip(request.remote_addr) or "127.0.0.1"
