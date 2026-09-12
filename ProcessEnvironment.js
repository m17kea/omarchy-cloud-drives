.pragma library

// Construct the environment before the executable's loader starts. Never
// inherit interpreter, loader, debug, proxy, rclone, or TLS override variables.
function build(getter, gui) {
  var names = [
    "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "LC_MESSAGES", "TZ", "TERM", "COLORTERM"
  ]
  if (gui) names = names.concat([
    "WAYLAND_DISPLAY", "DISPLAY", "XDG_SESSION_TYPE", "HYPRLAND_INSTANCE_SIGNATURE",
    "XDG_CURRENT_DESKTOP"
  ])
  var environment = { PATH: "/usr/bin:/bin", OMARCHY_PATH: "/usr/share/omarchy" }
  for (var i = 0; i < names.length; i++) {
    var value = getter(names[i])
    if (typeof value === "string" && value.length > 0) environment[names[i]] = value
  }
  return environment
}
