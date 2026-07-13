use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{Menu, MenuItemBuilder, SubmenuBuilder};
use tauri::Manager;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

#[cfg(not(debug_assertions))]
use tauri_plugin_shell::{process::CommandChild, ShellExt};

#[cfg(not(debug_assertions))]
type SidecarChild = CommandChild;
#[cfg(debug_assertions)]
type SidecarChild = (); // dev mode: ./start_server.sh owns the server

/// Holds the sidecar process so we can kill it on exit.
struct Backend(Mutex<Option<SidecarChild>>);

const DEFAULT_PORT: u16 = 8888;
const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
const REPO: &str = "TheDevRo/Jobsmith";

/// Where the backend keeps user state — mirrors `packaging/desktop_entry.py::app_home`.
fn app_home() -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME").unwrap_or_default();
        PathBuf::from(home).join("Library/Application Support/Jobsmith")
    }
    #[cfg(target_os = "windows")]
    {
        let base = std::env::var("APPDATA").unwrap_or_default();
        PathBuf::from(base).join("Jobsmith")
    }
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        let base = std::env::var("XDG_DATA_HOME").unwrap_or_else(|_| {
            format!(
                "{}/.local/share",
                std::env::var("HOME").unwrap_or_default()
            )
        });
        PathBuf::from(base).join("Jobsmith")
    }
}

/// Packaged builds have nowhere to print to — the sidecar's stdout/stderr goes
/// here so a failed launch is diagnosable after the fact.
fn shell_log_path() -> PathBuf {
    app_home().join("data").join("logs").join("shell.log")
}

#[cfg(not(debug_assertions))]
fn open_shell_log() -> Option<std::fs::File> {
    let path = shell_log_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .ok()
}

/// Prefer 8888; if another Jobsmith (web/Docker) already owns it, let the OS
/// hand us a free port instead of hard-failing on first launch. The probe
/// listener is dropped before the sidecar binds — the window for someone
/// else to steal the port is tiny and the failure mode is the existing one.
#[cfg(not(debug_assertions))]
fn pick_port() -> u16 {
    use std::net::TcpListener;
    if TcpListener::bind(("127.0.0.1", DEFAULT_PORT)).is_ok() {
        return DEFAULT_PORT;
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(DEFAULT_PORT)
}

/// A real HTTP readiness check, not just "something accepted a TCP connect".
/// `/api/health/live` answers 200 `{"status":"ok"}` without probing the AI
/// backend, so it flips true exactly when the API can serve the dashboard.
fn probe_health(addr: &str) -> bool {
    let sock: SocketAddr = match addr.parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&sock, Duration::from_millis(500)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1000)));
    let _ = stream.set_read_timeout(Some(Duration::from_millis(2000)));

    let req = format!(
        "GET /api/health/live HTTP/1.1\r\nHost: {}\r\nAccept: application/json\r\n\
         User-Agent: JobsmithDesktop\r\nConnection: close\r\n\r\n",
        addr
    );
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }

    let mut raw: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 1024];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&chunk[..n]);
                if raw.len() > 16 * 1024 {
                    break;
                }
            }
            Err(_) => break,
        }
    }

    let text = String::from_utf8_lossy(&raw);
    let status_ok = text
        .lines()
        .next()
        .map(|l| l.contains(" 200"))
        .unwrap_or(false);
    // Tolerate whatever spacing the JSON serializer chose.
    let compact: String = text.chars().filter(|c| !c.is_whitespace()).collect();
    status_ok && compact.contains("\"status\":\"ok\"")
}

/// Poll `/api/health/live` until it answers, the sidecar dies, or we time out.
fn wait_for_backend(addr: &str, timeout: Duration, sidecar_dead: &AtomicBool) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if sidecar_dead.load(Ordering::SeqCst) {
            return false;
        }
        if probe_health(addr) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

/// Replace the splash with a diagnosable failure screen: what happened, the
/// concrete log path, and a Retry that relaunches the app.
fn failure_script(detail: &str) -> String {
    let msg = serde_json::to_string(detail).unwrap_or_else(|_| "\"\"".into());
    let log = serde_json::to_string(&shell_log_path().to_string_lossy().to_string())
        .unwrap_or_else(|_| "\"\"".into());
    format!(
        r#"(function() {{
  var detail = {msg};
  var logPath = {log};
  document.body.innerHTML = '';
  var wrap = document.createElement('div');
  wrap.style.maxWidth = '520px';
  wrap.style.textAlign = 'center';
  var h = document.createElement('h2');
  h.textContent = 'Jobsmith backend failed to start';
  var p = document.createElement('p');
  p.className = 'hint';
  p.textContent = detail;
  var l = document.createElement('p');
  l.className = 'hint';
  l.style.wordBreak = 'break-all';
  l.textContent = 'Log: ' + logPath;
  var b = document.createElement('button');
  b.textContent = 'Retry';
  b.style.marginTop = '12px';
  b.style.padding = '8px 18px';
  b.style.borderRadius = '6px';
  b.style.border = '1px solid #3a393e';
  b.style.background = '#f0863a';
  b.style.color = '#141317';
  b.style.fontSize = '13px';
  b.style.cursor = 'pointer';
  b.onclick = function() {{
    b.disabled = true;
    b.textContent = 'Restarting…';
    if (window.__TAURI__ && window.__TAURI__.core) {{
      window.__TAURI__.core.invoke('restart_app');
    }} else {{
      location.reload();
    }}
  }};
  wrap.appendChild(h);
  wrap.appendChild(p);
  wrap.appendChild(l);
  wrap.appendChild(b);
  document.body.appendChild(wrap);
}})();"#,
        msg = msg,
        log = log
    )
}

/// Retry button on the failure screen.
#[tauri::command]
fn restart_app(app: tauri::AppHandle) {
    app.restart();
}

fn open_external(target: &str) {
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(target).spawn();
    #[cfg(target_os = "windows")]
    let _ = std::process::Command::new("cmd")
        .args(["/C", "start", "", target])
        .spawn();
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let _ = std::process::Command::new("xdg-open").arg(target).spawn();
}

/// "1.10.2" -> [1, 10, 2] so 0.2.10 > 0.2.9 (a string compare gets that wrong).
fn version_parts(v: &str) -> Vec<u64> {
    v.trim()
        .trim_start_matches('v')
        .split(['.', '-', '+'])
        .map(|p| p.parse::<u64>().unwrap_or(0))
        .collect()
}

fn is_newer(remote: &str, current: &str) -> bool {
    let (a, b) = (version_parts(remote), version_parts(current));
    for i in 0..a.len().max(b.len()) {
        let (x, y) = (
            a.get(i).copied().unwrap_or(0),
            b.get(i).copied().unwrap_or(0),
        );
        if x != y {
            return x > y;
        }
    }
    false
}

/// Minimum-viable updater (works on unsigned builds — no signing keys needed):
/// compare APP_VERSION to the GitHub latest-release API and offer the download
/// page. Uses `curl` rather than pulling in an HTTP stack.
fn check_for_updates(app: &tauri::AppHandle) {
    let app = app.clone();
    std::thread::spawn(move || {
        let url = format!("https://api.github.com/repos/{}/releases/latest", REPO);
        let out = std::process::Command::new("curl")
            .args([
                "-fsSL",
                "--max-time",
                "15",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "User-Agent: JobsmithDesktop",
                &url,
            ])
            .output();

        let tag = match out {
            Ok(o) if o.status.success() => serde_json::from_slice::<serde_json::Value>(&o.stdout)
                .ok()
                .and_then(|v| {
                    v.get("tag_name")
                        .and_then(|t| t.as_str())
                        .map(|s| s.to_string())
                }),
            _ => None,
        };

        let Some(tag) = tag else {
            app.dialog()
                .message(format!(
                    "Could not reach GitHub to check for updates.\n\nYou're running Jobsmith {}.\nReleases: https://github.com/{}/releases",
                    APP_VERSION, REPO
                ))
                .kind(MessageDialogKind::Warning)
                .title("Check for Updates")
                .blocking_show();
            return;
        };

        if is_newer(&tag, APP_VERSION) {
            let go = app
                .dialog()
                .message(format!(
                    "Jobsmith {} is available (you have {}).",
                    tag.trim_start_matches('v'),
                    APP_VERSION
                ))
                .title("Update available")
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Open Downloads".into(),
                    "Later".into(),
                ))
                .blocking_show();
            if go {
                open_external(&format!("https://github.com/{}/releases/latest", REPO));
            }
        } else {
            app.dialog()
                .message(format!("Jobsmith {} is the latest version.", APP_VERSION))
                .title("Check for Updates")
                .blocking_show();
        }
    });
}

/// Append a Jobsmith submenu to the platform-default menu (keeping Copy/Paste,
/// Hide, Quit, … intact).
fn install_menu(app: &tauri::App) -> tauri::Result<()> {
    let handle = app.handle();
    let updates = MenuItemBuilder::with_id("check_updates", "Check for Updates…").build(app)?;
    let logs = MenuItemBuilder::with_id("open_logs", "Open Logs Folder").build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "Reload")
        .accelerator("CmdOrCtrl+R")
        .build(app)?;
    let issue = MenuItemBuilder::with_id("report_issue", "Report an Issue…").build(app)?;

    let submenu = SubmenuBuilder::new(app, "Jobsmith")
        .items(&[&updates, &logs, &reload, &issue])
        .build()?;

    let menu = Menu::default(handle)?;
    menu.append(&submenu)?;
    app.set_menu(menu)?;

    app.on_menu_event(move |app, event| match event.id().as_ref() {
        "check_updates" => check_for_updates(app),
        "open_logs" => {
            let dir = app_home().join("data").join("logs");
            let _ = std::fs::create_dir_all(&dir);
            open_external(&dir.to_string_lossy());
        }
        "reload" => {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.eval("location.reload()");
            }
        }
        "report_issue" => open_external(&format!("https://github.com/{}/issues/new", REPO)),
        _ => {}
    });
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .manage(Backend(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![restart_app])
        .setup(|app| {
            install_menu(app)?;

            // Dev builds (`tauri dev`) expect ./start_server.sh on 8888 and
            // just point the webview at it via devUrl.
            #[cfg(debug_assertions)]
            let port = DEFAULT_PORT;

            // Release builds spawn the PyInstaller backend as a sidecar on a
            // port we choose; desktop_entry.py honors JOBSMITH_PORT.
            #[cfg(not(debug_assertions))]
            let port = pick_port();

            // Flipped when the sidecar exits, so the readiness poll can show
            // the failure screen immediately instead of waiting out the timeout.
            let sidecar_dead = Arc::new(AtomicBool::new(false));

            #[cfg(not(debug_assertions))]
            {
                let sidecar = app
                    .shell()
                    .sidecar("jobsmith-backend")?
                    .env("JOBSMITH_PORT", port.to_string())
                    // The sidecar watches this PID: a force-quit of the shell
                    // never fires RunEvent::Exit, and without it uvicorn would
                    // survive and keep holding the port.
                    .env("JOBSMITH_SHELL_PID", std::process::id().to_string());
                let (mut rx, child) = sidecar.spawn()?;
                *app.state::<Backend>().0.lock().unwrap() = Some(child);

                // Drain sidecar output so the pipe never blocks; in a packaged
                // build stdout goes nowhere, so persist it to shell.log.
                let dead = sidecar_dead.clone();
                tauri::async_runtime::spawn(async move {
                    use tauri_plugin_shell::process::CommandEvent;
                    let mut log = open_shell_log();
                    while let Some(event) = rx.recv().await {
                        match event {
                            CommandEvent::Stdout(line) | CommandEvent::Stderr(line) => {
                                let text = String::from_utf8_lossy(&line);
                                print!("[backend] {}", text);
                                if let Some(f) = log.as_mut() {
                                    let _ = write!(f, "{}", text);
                                    let _ = f.flush();
                                }
                            }
                            CommandEvent::Terminated(payload) => {
                                let text = format!(
                                    "[shell] backend exited (code={:?}, signal={:?})\n",
                                    payload.code, payload.signal
                                );
                                eprint!("{}", text);
                                if let Some(f) = log.as_mut() {
                                    let _ = write!(f, "{}", text);
                                    let _ = f.flush();
                                }
                                dead.store(true, Ordering::SeqCst);
                                break;
                            }
                            _ => {}
                        }
                    }
                });
            }

            // Once the server answers, swap the splash page for the real app.
            // Chromium now downloads in the background (desktop_entry.py), so
            // startup is bounded by uvicorn boot, not a 150 MB download.
            let addr = format!("127.0.0.1:{}", port);
            let url = format!("http://127.0.0.1:{}/", port);
            let window = app.get_webview_window("main").expect("main window");
            let dead = sidecar_dead.clone();
            std::thread::spawn(move || {
                if wait_for_backend(&addr, Duration::from_secs(60), &dead) {
                    let _ = window.eval(&format!("window.location.replace('{}')", url));
                } else if dead.load(Ordering::SeqCst) {
                    let _ = window.eval(&failure_script(
                        "The backend process exited during startup.",
                    ));
                } else {
                    let _ = window.eval(&failure_script(
                        "The backend did not answer on 127.0.0.1 within 60 seconds.",
                    ));
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                #[cfg(not(debug_assertions))]
                if let Some(child) = app_handle.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
                #[cfg(debug_assertions)]
                {
                    let _ = app_handle;
                }
            }
        });
}
