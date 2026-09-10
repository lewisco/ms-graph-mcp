"""Use public roots plus optional enterprise PEM roots on every Microsoft TLS hop."""

import ssl

import requests

from ms_graph_mcp.config import Settings


def create_ssl_context(settings: Settings) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=requests.certs.where())
    if settings.extra_ca_file is not None:
        # Add to public roots, rather than replacing them. Invalid PEM fails at startup.
        context.load_verify_locations(cafile=str(settings.extra_ca_file))
    return context


class EntraTLSAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, context: ssl.SSLContext):
        self.context = context
        super().__init__()

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, pool = super().build_connection_pool_key_attributes(request, True, cert)
        pool["ssl_context"] = self.context
        return host, pool
