import QtQuick
import Quickshell.Io
import qs.Ui
import qs.Commons

// Credentials travel over private pipes, never through argv or shell settings.
FocusScope {
  id: root
  property bool active: false
  property bool ready: false
  property var provider: ({})
  property color foreground: Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.5)
  readonly property string helper: decodeURIComponent(String(Qt.resolvedUrl("bin/icloud-onboarding.py")).replace(/^file:\/\//, ""))
  property string step: "welcome"
  property string message: ""
  property string challengeTitle: "Verify your account"
  property string challengeKind: "code"
  property bool challengePassword: false
  property bool smsAvailable: false
  property string pendingBegin: ""
  property int eventCount: 0
  property bool cancelled: false
  property bool preparationLaunched: false
  property bool folderError: false
  readonly property bool waiting: step === "working" || step === "mounting"
  implicitHeight: content.implicitHeight

  signal prepareRequested()
  signal refreshRequested()
  signal mountRequested()
  signal openFolderRequested()
  signal dismissed()

  function begin() {
    cancel()
    step = "welcome"
    message = ""
    preparationLaunched = false
    folderError = false
    emailField.text = ""
    Qt.callLater(focusStep)
  }

  function focusStep() {
    if (!active) return
    if (step === "account") emailField.forceActiveFocus()
    else if (step === "challenge" && challengeKind !== "approval") answerField.forceActiveFocus()
    else if (primaryButton.enabled) primaryButton.forceActiveFocus()
    else cancelButton.forceActiveFocus()
  }

  function cancel() {
    cancelled = true
    pendingBegin = ""
    passwordField.text = ""
    answerField.text = ""
    startupTimer.stop()
    if (bridge.running) {
      bridge.write(JSON.stringify({ op: "cancel" }) + "\n")
      cancelTimer.restart()
    }
  }

  function dismiss() { cancel(); dismissed() }

  function fail(text) {
    cancel()
    message = text
    step = "error"
  }

  function submitAccount() {
    if (bridge.running || !emailField.text.trim() || !passwordField.text) return
    cancelled = false
    eventCount = 0
    pendingBegin = JSON.stringify({ op: "begin", email: emailField.text.trim(), password: passwordField.text })
    passwordField.text = ""
    message = "Connecting securely to Apple…"
    step = "working"
    bridge.running = true
    startupTimer.restart()
  }

  function submitAnswer(overrideAnswer) {
    if (!bridge.running) return
    var answer = overrideAnswer !== undefined ? overrideAnswer : (challengeKind === "approval" ? "continue" : answerField.text)
    if (!answer || (overrideAnswer === undefined && challengeKind === "code" && answer.length !== 6)) return
    bridge.write(JSON.stringify({ op: "answer", answer: answer }) + "\n")
    answerField.text = ""
    message = "Checking with Apple…"
    step = "working"
  }

  function handleEvent(data) {
    if (cancelled || !active) return
    if (!data.trim()) return
    if (data.length > 8192 || ++eventCount > 128) {
      fail("Sign-in returned an unexpected response. Please try again.")
      return
    }
    try {
      var event = JSON.parse(data)
      var phase = String(event.phase || "")
      if (phase === "working") {
        message = String(event.message || "Connecting securely to Apple…").slice(0, 400)
        step = "working"
      } else if (phase === "challenge") {
        challengeKind = ["code", "approval", "answer"].indexOf(event.kind) !== -1 ? event.kind : "answer"
        challengeTitle = String(event.title || "Verify your account").slice(0, 100)
        message = String(event.message || "Approve the request on a trusted Apple device.").slice(0, 600)
        challengePassword = event.password === true
        smsAvailable = event.smsAvailable === true
        answerField.text = ""
        step = "challenge"
      } else if (phase === "connected") {
        passwordField.text = ""
        answerField.text = ""
        message = "Your account is connected. Opening your iCloud folder…"
        step = "mounting"
        refreshRequested()
        mountRequested()
      } else if (phase === "error") {
        fail(String(event.message || "Could not sign in. Please try again.").slice(0, 600))
      } else {
        fail("Sign-in returned an unexpected response. Please try again.")
      }
    } catch (e) {
      fail("Could not read the sign-in response. Please try again.")
    }
  }

  function mountFinished(ok, detail) {
    if (!active || step !== "mounting") return
    if (ok) {
      message = "Your iCloud Drive is ready in your file manager."
      step = "done"
    } else {
      message = detail || "Your account is saved, but the folder could not connect. Check your connection and try again."
      step = "mount-error"
    }
  }

  function openFinished(ok, detail) {
    if (!active || step !== "done") return
    folderError = !ok
    message = ok ? "Your iCloud Drive is ready in your file manager." : detail
  }

  function advance() {
    if (step === "welcome") {
      if (ready) step = "account"
      else if (!preparationLaunched) { preparationLaunched = true; prepareRequested() }
      else refreshRequested()
    } else if (step === "account") submitAccount()
    else if (step === "challenge") submitAnswer()
    else if (step === "error") { message = ""; step = ready ? "account" : "welcome" }
    else if (step === "mount-error") { message = "Opening your iCloud folder…"; step = "mounting"; mountRequested() }
    else if (step === "done") openFolderRequested()
  }

  onStepChanged: Qt.callLater(focusStep)
  onActiveChanged: if (!active) cancel()
  Keys.onEscapePressed: dismiss()
  Component.onDestruction: cancel()

  Timer { id: cancelTimer; interval: 1500; onTriggered: if (bridge.running) bridge.signal(15) }
  Timer {
    id: startupTimer
    interval: 10000
    onTriggered: if (root.pendingBegin !== "") root.fail("The sign-in helper could not start. Try again.")
  }

  Process {
    id: bridge
    command: ["python3", root.helper]
    stdinEnabled: true
    onStarted: {
      startupTimer.stop()
      if (root.cancelled || !root.active) {
        root.pendingBegin = ""
        write(JSON.stringify({ op: "cancel" }) + "\n")
        return
      }
      write(root.pendingBegin + "\n")
      root.pendingBegin = ""
    }
    stdout: SplitParser { onRead: function(data) { root.handleEvent(String(data)) } }
    // Safe failures arrive as JSON. Never display raw diagnostics.
    stderr: SplitParser { onRead: function(data) {} }
    onExited: function(exitCode) {
      cancelTimer.stop()
      startupTimer.stop()
      root.pendingBegin = ""
      if (!root.cancelled && root.active && (root.step === "working" || root.step === "challenge"))
        root.fail("Sign-in stopped before it finished. Please try again.")
    }
  }

  Column {
    id: content
    width: parent.width
    spacing: Style.space(16)

    PanelHero {
      width: parent.width
      title: root.step === "done" ? "You're connected" : "iCloud Drive"
      meta: root.step === "done" ? "At home on Omarchy" : (root.step === "challenge" ? "One more step" : "Your files, close at hand")
      foreground: root.foreground
      iconComponent: Component {
        Text { text: "󰅟"; color: root.foreground; font.family: Style.font.family; font.pixelSize: Style.font.display }
      }
    }

    Column {
      visible: root.step === "welcome"
      width: parent.width
      spacing: Style.space(12)
      Text {
        width: parent.width
        text: "Bring your iCloud files into your file manager. Open and edit them like any other file."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
      Text {
        width: parent.width
        text: root.ready ? "Have a trusted Apple device nearby for verification. Advanced Data Protection can stay on." : (root.preparationLaunched ? "Finish setup in the Omarchy window, then check again." : "First, a quick setup prepares the drive connection and protects your sign-in in your login keyring.")
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    Column {
      visible: root.step === "account"
      width: parent.width
      spacing: Style.space(10)
      Text {
        text: "Sign in to your Apple Account"
        textFormat: Text.PlainText
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        font.bold: true
      }
      TextField {
        id: emailField
        width: parent.width
        placeholderText: "Email address"
        foreground: root.foreground
        maximumLength: 320
        inputMethodHints: Qt.ImhEmailCharactersOnly | Qt.ImhNoPredictiveText
        KeyNavigation.tab: passwordField
        onAccepted: passwordField.forceActiveFocus()
      }
      TextField {
        id: passwordField
        width: parent.width
        placeholderText: "Apple Account password"
        foreground: root.foreground
        password: true
        maximumLength: 1024
        inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
        KeyNavigation.tab: primaryButton
        KeyNavigation.backtab: emailField
        onAccepted: root.submitAccount()
      }
      Text {
        width: parent.width
        text: "Use your regular Apple password, not an app-specific password. Your connection is protected in your login keyring."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    Column {
      visible: root.step === "challenge"
      width: parent.width
      spacing: Style.space(10)
      Text {
        width: parent.width
        text: root.challengeTitle
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        font.bold: true
      }
      Text {
        width: parent.width
        text: root.message
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
      TextField {
        id: answerField
        visible: root.challengeKind !== "approval"
        width: parent.width
        foreground: root.foreground
        placeholderText: root.challengeKind === "code" ? "Six-digit verification code" : "Your answer"
        password: root.challengePassword
        maximumLength: root.challengeKind === "code" ? 6 : 1024
        inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
        validator: RegularExpressionValidator { regularExpression: root.challengeKind === "code" ? /^[0-9]{0,6}$/ : /^[\s\S]*$/ }
        horizontalAlignment: root.challengeKind === "code" ? Text.AlignHCenter : Text.AlignLeft
        font.letterSpacing: root.challengeKind === "code" ? 6 : 0
        KeyNavigation.tab: root.smsAvailable ? smsButton : primaryButton
        onAccepted: root.submitAnswer()
      }
      Button {
        id: smsButton
        visible: root.challengeKind === "code" && root.smsAvailable
        text: "Send a code by SMS"
        focusable: true
        fontSize: Style.font.caption
        foreground: root.dim
        KeyNavigation.tab: primaryButton
        onClicked: root.submitAnswer("sms")
      }
    }

    Column {
      visible: root.waiting || root.step === "error" || root.step === "mount-error" || root.step === "done"
      width: parent.width
      spacing: Style.space(10)
      Text {
        width: parent.width
        text: root.message
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: root.step === "error" || root.step === "mount-error" || root.folderError ? Color.urgent : root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }
      Text {
        visible: root.step === "done"
        width: parent.width
        text: String(root.provider.path || "~/Cloud/iCloudDrive").replace(/^\/home\/[^/]+/, "~").slice(0, 1024)
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
        color: root.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    PanelSeparator { width: parent.width; foreground: root.foreground }

    Flow {
      width: parent.width
      spacing: Style.space(8)
      Button {
        id: primaryButton
        text: {
          if (root.step === "welcome") return root.ready ? "Continue" : (root.preparationLaunched ? "Check again" : "Prepare this computer")
          if (root.step === "account") return "Sign in"
          if (root.step === "challenge") return root.challengeKind === "approval" ? "I've approved it" : "Verify"
          if (root.step === "done") return "Open folder"
          if (root.step === "error" || root.step === "mount-error") return "Try again"
          return root.step === "mounting" ? "Opening folder…" : "Connecting…"
        }
        iconText: root.waiting ? "󰑐" : (root.step === "done" ? "󰉋" : "")
        iconSpinning: root.waiting
        bordered: true
        focusable: true
        foreground: root.foreground
        opacity: enabled ? 1 : 0.5
        KeyNavigation.tab: cancelButton
        KeyNavigation.backtab: root.step === "account" ? passwordField : (root.step === "challenge" && root.challengeKind !== "approval" ? (root.smsAvailable ? smsButton : answerField) : cancelButton)
        enabled: !root.waiting && (root.step !== "account" || (!bridge.running && emailField.text.trim().length > 0 && passwordField.text.length > 0))
          && (root.step !== "challenge" || root.challengeKind === "approval" || (root.challengeKind === "code" ? answerField.text.length === 6 : answerField.text.length > 0))
          && (root.step !== "error" || !bridge.running)
        onClicked: root.advance()
      }
      Button {
        id: cancelButton
        text: root.step === "done" ? "Done" : "Cancel"
        focusable: true
        foreground: root.dim
        KeyNavigation.backtab: primaryButton
        KeyNavigation.tab: root.step === "account" ? emailField : (root.step === "challenge" && root.challengeKind !== "approval" ? answerField : primaryButton)
        onClicked: root.dismiss()
      }
    }
  }
}
