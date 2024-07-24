"""
gs_fastcopy: optimized file transfer to and from Google Cloud Storage.

gs_fastcopy provides a stream interface around the XML Multipart Upload
API (which only works with files) for optimized reading and writing. It
also wraps the command-line tools `pigz` and `unpigz` (parallel gzip)
for faster (de)compression, if available.

See also the `read()` and `write()` functions.
"""

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager

from google.cloud import storage
from google.cloud.storage import transfer_manager


@contextmanager
def read(gs_uri):
    """
    Context manager for reading a file from Google Cloud Storage.

    Usage example, reading a numpy npz file:

    ```
    import gs_fastcopy
    import numpy as np

    with gs_fastcopy.read('gs://my-bucket/my-file.npz') as f:
        npz = np.load(f)
        a = npz['a']
        b = npz['b']
    ```

    This will download the file to a temporary directory, and open it
    for reading. When the 'with' block exits, the handle is closed and
    the temporary directory is deleted.

    If the gs_uri ends with '.gz', the file is decompressed before
    reading. Note that the decompression is performed in an external
    process, not streaming in memory. This means you need enough disk
    space for the compressed file, and the decompressed file, together.

    :param gs_uri: The Google Cloud Storage URI to read from.
    """
    with tempfile.TemporaryDirectory() as tmp:
        buffer_file_name = os.path.join(
            tmp, "download.gz" if gs_uri.endswith(".gz") else "download"
        )

        result = subprocess.run(
            ["gcloud", "storage", "cp", gs_uri, buffer_file_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # TODO: handle errors better than this
        if result.returncode != 0:
            raise Exception(
                f"Failed to download file from {gs_uri}: stderr: {result.stderr}"
            )

        # If necessary, decompress the file before reading.
        if buffer_file_name.endswith(".gz"):
            # unpigz is a parallel gunzip implementation that's
            # much faster when hardware is available.
            tool = "unpigz" if shutil.which("unpigz") else "gunzip"

            result = subprocess.run(
                [tool, buffer_file_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # TODO: handle errors better than this
            if result.returncode != 0:
                raise Exception(
                    f"Failed to extract file downloaded from {gs_uri}: stderr: {result.stderr}"
                )

            # Remove the '.gz' extension from the filename (like the tools do)
            buffer_file_name = buffer_file_name[:-3]

        with open(buffer_file_name, "rb") as f:
            yield f


@contextmanager
def write(gs_uri, max_workers=None, chunk_size=None):
    """
    Context manager for writing a file to Google Cloud Storage.

    Usage example, writing a numpy npz file:

    ```
    import gs_fastcopy
    import numpy as np

    with gs_fastcopy.write('gs://my-bucket/my-file.npz') as f:
        np.savez(f, a=np.zeros(12), b=np.ones(23))
    ```

    This will open a file for writing in a temporary directory. When
    the 'with' block exits, the file is uploaded to the specified
    Google Cloud Storage URI, and the temporary directory is deleted.

    If the gs_uri ends with '.gz', the file is compressed before
    uploading. Note that the compression is performed in an external
    process, not streaming in memory. This means you need enough disk
    space for the uncompressed file, and the compressed file, together.

    :param gs_uri: The Google Cloud Storage URI to write to.
    :param max_workers: The maximum number of workers to use. None for default (available CPUs).
    :param chunk_size: The size of each chunk to upload. None for default.
    """

    if max_workers is None:
        max_workers = _get_available_cpus()

    # Create a temporary scratch directory.
    # Will be deleted when the 'with' closes.
    with tempfile.TemporaryDirectory() as tmp_dir:
        # We need an actual filename within the scratch directory.
        buffer_file_name = os.path.join(tmp_dir, "file_to_upload")

        # Yield the file object for the caller to write.
        with open(buffer_file_name, "wb") as tmp_file:
            yield tmp_file

        # If requested, compress the file before uploading.
        if gs_uri.endswith(".gz"):
            # pigz is a parallel gzip implementation that's
            # much faster when hardware is available.
            tool = "pigz" if shutil.which("pigz") else "gzip"

            # TODO: handle errors
            result = subprocess.run(
                [tool, buffer_file_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # TODO: handle errors better than this
            if result.returncode != 0:
                raise Exception(
                    f"Failed to compress file for upload to {gs_uri}: stderr: {result.stderr}"
                )

            # Add the '.gz' extension to the filename (like the tools do)
            buffer_file_name += ".gz"

        args = {"max_workers": max_workers}
        if chunk_size is not None:
            args["chunk_size"] = chunk_size

        # Parse gs_uri into a blob
        client = storage.Client()
        gs_blob = storage.Blob.from_string(gs_uri, client=client)

        # TODO: handle errors in transfer_manager
        transfer_manager.upload_chunks_concurrently(buffer_file_name, gs_blob, **args)


# Helper function to get the number of available CPUs.
# On many Unixen, available CPUs can be restricted
# using schedule affinity; that is a more accurate
# measure of available CPUs if available. If not,
# fall back to os.cpu_count().
def _get_available_cpus():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count()
