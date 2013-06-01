import pwn, sys, time, os, tty, termios, paramiko, re, tempfile, datetime, shutil, socket, getpass
from pwn import log, text
from subprocess import Popen, PIPE
from basechatter import basechatter
from select import select

class WarnPolicy(paramiko.MissingHostKeyPolicy):
    '''Policy for what happens when an unknown ssh-fingerprint is encountered'''
    def __init__(self):
        self.do_warning = False

    def missing_host_key(self, client, hostname, key):
        self.do_warning = True

class ssh_channel(basechatter):
    def __init__(self, parent, process = None, silent = None, tty = True):
        self.parent = parent
        self._tty = tty
        self._channel = None
        self.exit_status = None
        if silent == None:
            silent = parent.silent
        basechatter.__init__(self, self.parent.timeout, silent)
        self._connect(process)

    def _connect(self, process = None):
        if self.connected():
            log.warning('SSH channel is already connected')
            return

        if not self.silent:
            log.waitfor('Opening new channel: "%s"' % (process or 'shell'))

        self._channel = self.parent._transport.open_session()
        if self._tty:
            width, height = pwn.get_term_size()
            self._channel.get_pty('vt100', width, height)
        self._channel.settimeout(self.timeout)

        # If you ever need stderr with pwntools, then you are likely doing something wrong... AMIRITE?
        self._channel.set_combine_stderr(True)

        if process:
            self._channel.exec_command(process)
        else:
            self._channel.invoke_shell()

        if not self.silent:
            log.succeeded()

    def connected(self):
        '''Returns True if the channel is connected.'''
        return self._channel != None

    def close(self):
        '''Closes the channel.'''
        if self._channel:
            if self._channel.exit_status_ready():
                self.exit_status = self._channel.recv_exit_status()
            self._channel.close()
            self._channel = None

    def _send(self, dat):
        while dat:
            n = self._channel.send(dat)
            dat = dat[n:]

    def _recv(self, numb):
        end_time = time.time() + self.timeout

        while True:
            r = ''
            if not self.connected():
                break
            if self._channel.exit_status_ready() and not self._channel.recv_ready():
                self.close()
                break
            try:
                r = self._channel.recv(numb)
            except IOError as e:
                if e.errno != 11:
                    raise
            except socket.timeout:
                pass

            if r or time.time() > end_time:
                break
            time.sleep(0.0001)
        return r

    def fileno(self):
        '''Returns the underlying file number for the channel.'''
        return self._channel.fileno()

    def can_recv(self, timeout = None):
        '''Returns True if the channel is ready to recieve (the timeout is ignored).'''
        return self._channel.recv_ready()

    def interactive(self, prompt = text.boldred('$') + ' '):
        '''Turns the channel into an interactive session (that is, it connects stdin and stdout to the channel).'''
        if not self._tty:
            basechatter.interactive(self, prompt)
            return

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setraw(fd)

        try:
            while True:
                reads, _, _ = select([sys.stdin.fileno(), self._channel.fileno()], [], [], 0.05)

                while self._channel.recv_ready():
                    dat = self.recv()
                    sys.stdout.write(dat)
                    sys.stdout.flush()

                if self._channel.exit_status_ready():
                    if not self._channel.recv_ready():
                        break
                elif sys.stdin.fileno() in reads:
                    dat = sys.stdin.read(1)

                    # Break if ctrl+] is hit
                    if dat == '\x1d':
                        sys.stdout.write('\r\n')
                        sys.stdout.flush()
                        break

                    self.send(dat)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

class ssh:
    def __init__(self, host, user = None, password = None, port = None, silent = False, key = None, keyfile = None, proxy_command = None, proxy_sock = None, timeout = 'default'):
        '''Creates a new ssh connection.

        Most argumnts are self-explanatory and unnecessary in most cases.

        In addition to the specified arguments, it also tries to act like the
        command line tool would. This means that it parses ~/.ssh/config to
        some extent, uses ~/.ssh/known_hosts to validate the connection and
        looks for ~/.ssh/id_rsa and ~/.ssh/id_dsa (but also ./id_rsa and
        ./id_dsa).

        The host takes the form [user[:password]@]hostname[:port].

        A common use-case is the following:

            r = ROP('isvuln')

            ssh = ssh('root@some.host.na.me')
            ssh.upload('/tmp/some_file', raw = 'data data data')
            ssh.libs('/home/sploitme/isvuln', rop = r)
            sock = ssh.run('/home/sploitme/isvuln /tmp/some_file')

        NOTE: The proxy_command and proxy_sock arguments and the ProxyCommand
        option in ~/.ssh/config is only available if a fairly new version of
        paramiko is used.'''
        # Save arguments
        self._user = user
        self._password = password
        self._port = port
        self._key = key
        self.silent = silent
        self._keyfiles = ['id_rsa', 'id_dsa']

        if keyfile:
            if not hasattr(keyfile, '__iter__'):
                self._keyfiles.insert(0, keyfile)
            else:
                self._keyfiles = list(keyfile) + self._keyfiles

        self._proxy_command = proxy_command
        self._proxy_sock = proxy_sock

        # This is an ugly hack to use the same semantics for
        # the timeout as is done for basechatter
        b = basechatter(timeout)
        self.timeout = b.timeout

        # Parse the host string, which can overwrite the arguments
        self._parse_host(host)

        # Initialize variables
        self._client = None
        self._sftp = None

        # Connect to the ssh server
        self._connect()

    def _parse_host(self, host):
        # Split off the optional authentication
        host_ = host.split('@', 1)
        if len(host_) == 1:
            auth, host_ = None, host_[0]
        else:
            auth, host_ = host_

        # Parse the authentication
        if auth:
            auth_ = auth.split(':', 1)
            if len(auth_) == 1:
                self._user = auth_[0]
            else:
                self._user, self._password = auth_

        # Parse the optional port
        host_ = host_.split(':', 1)

        if len(host_) == 1:
            self.host = host_[0]
        else:
            self.host, port = host_

            if not (port and port.isdigit()):
                pwn.die('Port "%s" is not a number' % port)

            self._port = int(port)

    def _connect(self):
        if self.connected():
            log.warning('SSH connection to "%s" already started' % self.host)
            return
        if not self.silent:
            log.waitfor('Starting SSH connection to "%s"' % self.host)

        conf = paramiko.SSHConfig()
        conf.parse(open(os.path.expanduser('~/.ssh/config')))
        conf = conf.lookup(self.host)

        if not conf:
            conf = {}

        self._port = self._port or int(conf.get('port', '22'))
        self.host = conf.get('hostname', self.host)
        self._user = self._user or conf.get('user', getpass.getuser())
        self._proxy_command = self._proxy_command or conf.get('proxycommand')

        if self._proxy_command and self._proxy_command.lower() == 'none':
            self._proxy_command = None

        if 'identityfile' in conf:
            self._keyfiles.insert(0, conf['identityfile'])

        self._keyfiles = [os.path.expanduser(k) for k in self._keyfiles]
        self._keyfiles = [k for k in self._keyfiles if os.path.exists(k)]

        self._client = paramiko.SSHClient()
        p = WarnPolicy()
        self._client.set_missing_host_key_policy(p)
        self._client.load_system_host_keys()

        has_proxy = (self._proxy_sock or self._proxy_command) and True
        if has_proxy and 'ProxyCommand' in dir(paramiko):
            if self._proxy_command and not self._proxy_sock:
                self._proxy_command = self._proxy_command.replace('%h', self.host).replace('%p', str(self._port)).replace('%r', self._user)
                self._proxy_sock = paramiko.ProxyCommand(self._proxy_command)
            self._client.connect(self.host, self._port, self._user, self._password, self._key, self._keyfiles, self.timeout, compress = True, sock = self._proxy_sock)
        else:
            if has_proxy:
                log.warning('This version of paramiko does not support proxies. Ignoring the specified proxy.')
            self._client.connect(self.host, self._port, self._user, self._password, self._key, self._keyfiles, self.timeout, compress = True)

        self._transport = self._client.get_transport()

        if not self.silent:
            log.succeeded()

            if p.do_warning:
                log.warning('SSH key could not be validated')


    def shell(self, silent = None, tty = True):
        '''Open a new channel with a shell inside.'''
        return ssh_channel(self, silent = silent, tty = tty)

    def run(self, process, silent = None, tty = False):
        '''Open a new channel with a specific process inside.'''
        return ssh_channel(self, process, silent, tty = tty)

    def run_simple(self, process, tty = False):
        '''Run a command on the remote server and return a tuple with (data, exit_status).'''
        c = self.run(process, silent = True, tty = tty)
        dat = c.recvall()
        return dat, c.exit_status

    def connected(self):
        '''Returns True if we are connected.'''
        return self._client != None

    def close(self):
        '''Close the connection.'''
        if self._client:
            self._client.close()
            self._client = None

    def _libs_remote(self, remote):
        '''Return a dictionary of the libraries used by a remote file.'''
        dat, status = self.run_simple('ldd "$(echo %s|base64 -d)"' % pwn.b64(remote))
        if status != 0:
            log.warning('Unable to find libraries for "%s"' % remote)
            return {}

        return pwn.parse_ldd_output(dat)

    def _get_fingerprint(self, remote):
        dat, status = self.run_simple('sha256sum "$(echo %s|base64 -d)"' % pwn.b64(remote))
        if status == 0:
            return dat.split()[0]

        dat, status = self.run_simple('sha1sum "$(echo %s|base64 -d)"' % pwn.b64(remote))
        if status == 0:
            return dat.split()[0]

        dat, status = self.run_simple('md5sum "$(echo %s|base64 -d)"' % pwn.b64(remote))
        if status == 0:
            return dat.split()[0]

        return None

    def _get_cachefile(self, fingerprint):
        return os.path.join(self._cachedir, fingerprint)

    def _verify_local_fingerprint(self, fingerprint):
        if not isinstance(fingerprint, str) or \
           len(fingerprint) not in [32, 40, 64] or \
           re.match('[^a-f0-9]', fingerprint):
            log.warning('Invalid fingerprint "%s"' % fingerprint)
            return False

        local = self._get_cachefile(fingerprint)
        if not os.path.isfile(local):
            return False

        func = {32: pwn.md5filehex, 40: pwn.sha1filehex, 64: pwn.sha256filehex}[len(fingerprint)]

        if func(local) == fingerprint:
            return True
        else:
            os.unlink(local)
            return False

    def _initialize_sftp(self):
        if self._sftp == None:
            self._sftp = self._client.open_sftp()

        self._cachedir = os.path.join(tempfile.gettempdir(), 'pwn-ssh-cache')

        if not os.path.isdir(self._cachedir):
            try:
                os.mkdir(self._cachedir)
            except:
                pwn.die('Could not create ssh cache dir: %s' % self._cachedir)

    def _download_raw(self, remote, local):
        self._initialize_sftp()

        if not self.silent:
            log.waitfor('Downloading %s' % remote)

        def update(has, total):
            if not self.silent:
                log.status("%s/%s" % (pwn.size(has), pwn.size(total)))

        self._sftp.get(remote, local, update)

        if not self.silent:
            log.succeeded()

    def _download_to_cache(self, remote):
        self._initialize_sftp()
        fingerprint = self._get_fingerprint(remote)
        if fingerprint == None:
            local = os.path.normpath(remote)
            local = os.path.basename(local)
            local += datetime.strftime('-%Y-%m-d-%H:%M:%S')
            local = os.path.join(self._cachedir, local)

            self._download_raw(remote, local)
            return local

        local = self._get_cachefile(fingerprint)

        if self._verify_local_fingerprint(fingerprint):
            if not self.silent:
                log.success('Found %s in ssh cache' % remote)
        else:
            self._download_raw(remote, local)

            if not self._verify_local_fingerprint(fingerprint):
                pwn.die('Could not download file "%s"' % remote)

        return local

    def download(self, remote, local = None, raw = False):
        '''Downloads a file from the remote server.

        The file is cached in /tmp/pwn-ssh-cache using a hash of the file, so
        calling the function twice has little overhead.

        Set raw to True, if you want the data returned instead of saved to a
        file.

        If local is None and the data is to be saved, then the local filename
        is inferred from the remote.'''

        local_tmp = self._download_to_cache(remote)

        if raw:
            return pwn.read(local_tmp)

        if not local:
            local = os.path.basename(os.path.normpath(remote))

        shutil.copy2(local_tmp, local)

    def libs(self, remote, dir = None, rop = None):
        '''Downloads the libraries referred to by a file.

        This is done by running ldd on the remote server, parsing the output
        and downloading the relevant files.

        The dir argument specified where to download the files. This defaults
        to './$HOSTNAME' where $HOSTNAME is the hostname of the remote server.

        Set rop to a rop-object to update it's list of known libraries.'''

        libs = self._libs_remote(remote)

        if dir == None:
            dir = self.host

        dir = os.path.realpath(dir)

        res = {}

        seen = set([])

        for lib, remote in libs.items():
            if not remote or lib == 'linux':
                continue

            local = os.path.realpath(os.path.join(dir, '.' + os.path.sep + remote))
            if not local.startswith(dir):
                pwn.warning('This seems fishy: %s' % remote)
                continue

            dir2 = os.path.dirname(local)

            if not os.path.exists(dir2):
                try:
                    os.makedirs(dir2)
                except:
                    pwn.die('Could not create dir: %s' % dir2)

            if remote not in seen:
                self.download(remote, local)
                seen.add(remote)
            res[lib] = local

        if rop:
            rop.extra_libs(res)

        return res

    def upload(self, remote = None, local = None, raw = None):
        '''Uploads a file to the remote server.

        If remote is set to None, then the remote filename is inferred from the
        local filename.

        If raw is None, then the file specified by local is uploaded.
        Otherwise the data in the raw variable is uploaded instead.'''

        self._initialize_sftp()

        if remote == None:
            remote = os.path.normpath(local)
            remote = os.path.basename(remote)

        if raw == None:
            self._sftp.put(local, remote)
        else:
            f = self._sftp.open(remote, 'wb')
            f.write(raw)
            f.close()