use std::net::TcpStream;
use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;

#[cfg(not(debug_assertions))]
use tauri_plugin_shell::{process::CommandChild, ShellExt};

#[cfg(not(debug_assertions))]
type SidecarChild = CommandChild;
#[cfg(debug_assertions)]
type SidecarChild = (); // dev mode: ./start_server.sh owns the server

/// Holds the sidecar process so we can kill it on exit.
struct Backend(Mutex<Option<SidecarChild>>);

const BACKEND_ADDR: &str = "127.0.0.1:8888";
const BACKEND_URL: &str = "http://127.0.0.1:8888/";

fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if TcpStream::connect_timeout(&BACKEND_ADDR.parse().unwrap(), Duration::from_millis(500))
            .is_ok()
        {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            // Release builds spawn the PyInstaller backend as a sidecar.
            // Dev builds (`tauri dev`) expect ./start_server.sh to be running
            // and just point the webview at it via devUrl.
            #[cfg(not(debug_assertions))]
            {
                let sidecar = app.shell().sidecar("jobsmith-backend")?;
                let (mut rx, child) = sidecar.spawn()?;
                *app.state::<Backend>().0.lock().unwrap() = Some(child);

                // Drain sidecar output so the pipe never blocks; forward to log.
                tauri::async_runtime::spawn(async move {
                    use tauri_plugin_shell::process::CommandEvent;
                    while let Some(event) = rx.recv().await {
                        if let CommandEvent::Stdout(line) | CommandEvent::Stderr(line) = event {
                            print!("[backend] {}", String::from_utf8_lossy(&line));
                        }
                    }
                });
            }

            // Once the server answers, swap the splash page for the real app.
            // First launch can take minutes (Chromium download), so poll long.
            let window = app.get_webview_window("main").expect("main window");
            std::thread::spawn(move || {
                if wait_for_backend(Duration::from_secs(600)) {
                    let _ = window.eval(&format!("window.location.replace('{}')", BACKEND_URL));
                } else {
                    let _ = window.eval(
                        "document.body.innerHTML = '<h2 style=\"font-family:sans-serif\">\
                         Jobsmith backend failed to start. Check the logs and relaunch.</h2>'",
                    );
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
