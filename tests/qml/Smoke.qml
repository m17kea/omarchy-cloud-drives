import QtQuick
import Quickshell
import "ProcessEnvironment.js" as ProcessEnvironment

Scope {
  id: test
  property var wizard: null
  property var authWindow: null
  property int stage: 0
  property int ticks: 0

  function check(ok) {
    if (!ok) { Qt.exit(1); throw new Error("QML smoke assertion failed") }
  }

  function field(node, placeholder) {
    if ("placeholderText" in node && node.placeholderText === placeholder) return node
    var children = node.children || []
    for (var i = 0; i < children.length; i++) {
      var found = field(children[i], placeholder)
      if (found) return found
    }
    return null
  }

  Component.onCompleted: {
    var injected = { HOME: "/home/example", PATH: "/untrusted", OMARCHY_PATH: "/untrusted",
      LD_PRELOAD: "/untrusted.so", PYTHONPATH: "/untrusted", QT_PLUGIN_PATH: "/untrusted",
      QML_IMPORT_PATH: "/untrusted", QS_CONFIG_PATH: "/untrusted", BASH_ENV: "/untrusted",
      HTTPS_PROXY: "http://invalid", SSL_CERT_FILE: "/untrusted", RCLONE_CONFIG: "/untrusted",
      LC_EXTRA: "invalid", LC_CTYPE: "C.UTF-8", TERM: "xterm-256color", WAYLAND_DISPLAY: "wayland-test" }
    var getter = function(name) { return injected[name] }
    var base = ProcessEnvironment.build(getter, false)
    check(base.PATH === "/usr/bin:/bin" && base.OMARCHY_PATH === "/usr/share/omarchy")
    check(base.HOME === "/home/example" && base.TERM === "xterm-256color" && base.LC_CTYPE === "C.UTF-8")
    var forbidden = ["LD_PRELOAD", "PYTHONPATH", "QT_PLUGIN_PATH", "QML_IMPORT_PATH", "QS_CONFIG_PATH",
      "BASH_ENV", "HTTPS_PROXY", "SSL_CERT_FILE", "RCLONE_CONFIG", "LC_EXTRA", "WAYLAND_DISPLAY"]
    for (var i = 0; i < forbidden.length; i++) check(base[forbidden[i]] === undefined)
    check(ProcessEnvironment.build(getter, true).WAYLAND_DISPLAY === "wayland-test")

    var component = Qt.createComponent("ICloudSetup.qml")
    check(component.status === Component.Ready)
    wizard = component.createObject(test, { active: true, ready: true,
      helper: Quickshell.env("CLOUD_DRIVES_PLUGIN_DIR") + "/bin/icloud-onboarding.py" })
    check(wizard !== null)
    wizard.begin()
    wizard.advance()
    var password = field(wizard, "Apple Account password")
    check(password.password && password.passwordMaskDelay === 0)
    field(wizard, "Email address").text = "verify@example.test"
    password.text = "not-a-real-secret"
    wizard.submitAccount()
    check(password.text.length === 0)
  }

  Timer {
    interval: 100
    running: true
    repeat: true
    onTriggered: {
      if (++test.ticks > 80) { Qt.exit(3); return }
      var wizard = test.wizard
      if (test.stage === 0 && wizard.step === "challenge") {
        test.check(wizard.pendingBegin.length === 0)
        var code = test.field(wizard, "Six-digit verification code")
        test.check(code.password && code.passwordMaskDelay === 0)
        code.text = "123456"
        wizard.submitAnswer()
        test.check(code.text.length === 0)
        test.stage = 1
      } else if (test.stage === 1 && wizard.step === "mounting" && !wizard.helperRunning) {
        wizard.begin()
        wizard.advance()
        test.field(wizard, "Email address").text = "cancel@example.test"
        test.field(wizard, "Apple Account password").text = "mock-cancel-only"
        wizard.submitAccount()
        test.stage = 2
      } else if (test.stage === 2 && wizard.helperRunning && wizard.pendingBegin.length === 0) {
        wizard.cancel()
        test.check(test.field(wizard, "Apple Account password").text.length === 0)
        test.stage = 3
      } else if (test.stage === 3 && !wizard.helperRunning) {
        test.check(wizard.pendingBegin.length === 0)
        test.stage = 4
        var component = Qt.createComponent("shell.qml")
        test.check(component.status === Component.Ready)
        test.authWindow = component.createObject(test)
        test.check(test.authWindow !== null)
      } else if (test.stage === 4 && test.authWindow.ready) {
        test.authWindow.requestClose()
      }
    }
  }
}
