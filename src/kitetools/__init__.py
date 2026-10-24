import truststore

# Local antivirus (Norton) re-signs HTTPS traffic; trust the Windows certificate store so verification still succeeds.
truststore.inject_into_ssl()
