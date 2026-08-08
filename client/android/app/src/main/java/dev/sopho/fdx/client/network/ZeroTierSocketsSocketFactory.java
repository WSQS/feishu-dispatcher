package dev.sopho.fdx.client.network;

import com.zerotier.sockets.ZeroTierSocket;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.Socket;
import java.net.SocketAddress;
import java.net.SocketException;
import java.net.UnknownHostException;
import java.nio.channels.SocketChannel;

import javax.net.SocketFactory;

/**
 * 让 OkHttp 的 socket 走 libzt 的 {@link ZeroTierSocket}。抄自 libzt-android-examples
 * 的 okhttpclient demo（原包 com.zerotier.sockets，改到本包 + import ZeroTierSocket）。
 *
 * OkHttp 只调这几个：{@link #createSocket()}、{@link SocketProxy#connect(SocketAddress, int)}、
 * {@link SocketProxy#close()}、{@link SocketProxy#getInputStream()}、{@link SocketProxy#getOutputStream()}、
 * {@link SocketProxy#isClosed()}、{@link SocketProxy#isInputShutdown()}、{@link SocketProxy#isOutputShutdown()}、
 * {@link SocketProxy#setSoTimeout(int)}。其余方法抛 RuntimeException（OkHttp 不调）。
 */
public class ZeroTierSocketsSocketFactory extends SocketFactory {

    String remoteAddr;
    int port;

    public ZeroTierSocketsSocketFactory(String remoteAddr, int port) {
        this.remoteAddr = remoteAddr;
        this.port = port;
    }

    @Override
    public Socket createSocket() throws IOException, UnknownHostException {
        return new SocketProxy(remoteAddr, port);
    }

    @Override
    public Socket createSocket(String host, int port) throws IOException, UnknownHostException {
        throw new RuntimeException("Unimplemented");
    }

    @Override
    public Socket createSocket(String host, int port, InetAddress localHost, int localPort) throws IOException, UnknownHostException {
        throw new RuntimeException("Unimplemented");
    }

    @Override
    public Socket createSocket(InetAddress host, int port) throws IOException {
        throw new RuntimeException("Unimplemented");
    }

    @Override
    public Socket createSocket(InetAddress address, int port, InetAddress localAddress, int localPort) throws IOException {
        throw new RuntimeException("Unimplemented");
    }

    static class SocketProxy extends Socket {

        String remoteAddr;
        int port;
        //
        // creating new ZeroTierSocket() tries to connect immediately,
        // so delay creating ZeroTierSocket() until connect()
        //
        ZeroTierSocket sock;

        public SocketProxy(String remoteAddr, int port) {

            // Socket logic

            this.remoteAddr = remoteAddr;
            this.port = port;
        }

        @Override
        public void bind(SocketAddress bindpoint) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void close() throws IOException {

            //
            // called by OkHttp
            //

            if (sock != null) {
                sock.close();
            }
        }

        @Override
        public void connect(SocketAddress endpoint) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void connect(SocketAddress endpoint, int timeout) throws IOException {

            //
            // called by OkHttp
            //

            //
            // endpoint is something like: www.example.com/93.184.216.34:80
            //

            //
            // timeout is something like 10000
            //

            sock = new ZeroTierSocket(remoteAddr, port);
        }

        @Override
        public SocketChannel getChannel() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public InetAddress getInetAddress() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public InputStream getInputStream() throws SocketException {

            //
            // called by OkHttp
            //

            return sock.getInputStream();
        }

        @Override
        public boolean getKeepAlive() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public InetAddress getLocalAddress() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public int getLocalPort() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public SocketAddress getLocalSocketAddress() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public boolean getOOBInline() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public OutputStream getOutputStream() throws SocketException {

            //
            // called by OkHttp
            //

            return sock.getOutputStream();
        }

        @Override
        public int getPort() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public int getReceiveBufferSize() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public SocketAddress getRemoteSocketAddress() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public boolean getReuseAddress() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public int getSendBufferSize() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public int getSoLinger() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public int getSoTimeout() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public boolean getTcpNoDelay() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public int getTrafficClass() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public boolean isBound() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public boolean isClosed() {

            //
            // called by OkHttp
            //

            return sock == null || sock.isClosed();
        }

        @Override
        public boolean isConnected() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public boolean isInputShutdown() {

            //
            // called by OkHttp
            //

            return sock == null || sock.inputStreamHasBeenShutdown();
        }

        @Override
        public boolean isOutputShutdown() {

            //
            // called by OkHttp
            //

            return sock == null || sock.outputStreamHasBeenShutdown();
        }

        @Override
        public void sendUrgentData(int data) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setKeepAlive(boolean on) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setOOBInline(boolean on) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setPerformancePreferences(int connectionTime, int latency, int bandwidth) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setReceiveBufferSize(int size) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setReuseAddress(boolean on) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setSendBufferSize(int size) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setSoLinger(boolean on, int linger) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setSoTimeout(int timeout) throws SocketException {

            //
            // called by OkHttp
            //

            if (sock == null) {
                return;
            }

            sock.setSoTimeout(timeout);
        }

        @Override
        public void setTcpNoDelay(boolean on) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void setTrafficClass(int tc) {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void shutdownInput() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public void shutdownOutput() {
            throw new RuntimeException("Unimplemented");
        }

        @Override
        public String toString() {
            return "proxy(" + (sock == null ? "" : sock.toString()) + ")";
        }
    }
}
