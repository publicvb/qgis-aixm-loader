def classFactory(iface):
    from .plugin import AixmLoaderPlugin
    return AixmLoaderPlugin(iface)
