package android.net;

/** Host test double for the WebSocket probe checks: sockets stay on the default route (loopback). */
public class Network {
    public void bindSocket(java.net.Socket socket) {}
    public void bindSocket(java.net.DatagramSocket socket) {}
}
