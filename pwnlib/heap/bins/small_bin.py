from .bin import *


class SmallBins(Bins):

    def _name(self):
        return "Small Bins"


class SmallBin(Bin):
    """Class to represent an small bin of the glibc
    """

    def __init__(self, bin_entry, malloc_chunks):
        super(SmallBin, self).__init__(bin_entry, malloc_chunks)

    def __repr__(self):

        msg = "Smallbin [size = {:#x}, count = {}] => {:#x}".format(
            self.chunks_size,
            len(self),
            self.fd
        )

        for chunk in self.malloc_chunks:
            msg += chunk.to_bin_str()

        return msg

    def _name(self):
        return "Small Bin"


class SmallBinEntry(BinEntry):
    """Class to contain the information of a small bin entry in `bins` of
    malloc state.
    """

    def __init__(self, address, fd, bk, chunks_size):
        super(SmallBinEntry, self).__init__(
            address, fd, bk=bk, chunks_size=chunks_size)

    def __str__(self):
        return "Small Bin [{:#x}]: fd={:#x}, bk={:#x}".format(
            self.chunks_size, self.fd, self.bk
        )