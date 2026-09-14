import RFB from "/static/vendor/novnc/core/rfb.js";

class ScopeVncClient {
  constructor(container, onStatus) {
    this.container = container;
    this.onStatus = onStatus || (() => {});
    this.rfb = null;
  }

  connect(url, credentials) {
    this.disconnect();
    this.onStatus("connecting", "Opening interactive VNC session…");
    const rfb = new RFB(this.container, url, {
      credentials: {
        username: credentials.username,
        password: credentials.password,
      },
      shared: true,
    });
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.viewOnly = false;
    rfb.focusOnClick = true;
    rfb.background = "#000";
    rfb.addEventListener("connect", () => {
      this.onStatus("connected", "Live VNC connected — keyboard and pointer control are active.");
    });
    rfb.addEventListener("disconnect", (event) => {
      const clean = Boolean(event.detail?.clean);
      this.onStatus(
        clean ? "disconnected" : "error",
        clean ? "VNC disconnected." : "VNC connection failed or was closed by the scope.",
      );
    });
    rfb.addEventListener("credentialsrequired", () => {
      rfb.sendCredentials({
        username: credentials.username,
        password: credentials.password,
      });
    });
    rfb.addEventListener("securityfailure", (event) => {
      this.onStatus("error", event.detail?.reason || "VNC authentication failed.");
    });
    this.rfb = rfb;
  }

  disconnect() {
    if (!this.rfb) return;
    try {
      this.rfb.disconnect();
    } catch (_) {
      // The remote endpoint may already be gone.
    }
    this.rfb = null;
  }
}

window.ScopeVncClient = ScopeVncClient;
