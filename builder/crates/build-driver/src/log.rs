//! The run log: the build's event stream as it goes to stdout, also written to the work folder's `build.log` (and a
//! file the caller names), whole lines at a time, without the player's home folder and name (the game's rule:
//! crate::redact; players attach the log to build problems; stdout, which a launcher reads, stays as it is).

use crate::redact::Who;
use std::io::Write;
use std::path::Path;

/// `line` as a log keeps it: without the user's home folder and name ([`Who::redact`]), in a JSON line's escaped form
/// too (a Windows home folder's backslashes doubled).
pub fn for_log(who: &Who, line: &str) -> String {
    let s = who.redact(line);
    if who.windows && who.home.contains('\\') {
        let json = Who { home: who.home.replace('\\', "\\\\"), ..who.clone() };
        return json.redact(&s);
    }
    s
}

/// stdout, and the log files.
pub struct Tee {
    out: Box<dyn Write + Send>,
    files: Vec<std::fs::File>,
    line: Vec<u8>,
    who: Who,
}

impl Tee {
    /// `out` and the open `file`, written on (a panic's line added to a log).
    pub fn append(out: Box<dyn Write + Send>, file: std::fs::File) -> Tee {
        Tee { out, files: vec![file], line: Vec::new(), who: Who::here() }
    }

    /// stdout and `files` (each made again; one that cannot be opened is reported and left out).
    pub fn new(out: Box<dyn Write + Send>, files: &[&Path], mut report: impl FnMut(String)) -> Tee {
        let mut opened = Vec::new();
        for f in files {
            if let Some(dir) = f.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            match std::fs::File::create(f) {
                Ok(h) => opened.push(h),
                Err(e) => report(format!("the build log {} was not opened: {e}", f.display())),
            }
        }
        Tee { out, files: opened, line: Vec::new(), who: Who::here() }
    }

    fn redact(&self, line: &str) -> String {
        for_log(&self.who, line)
    }

    fn write_line(&mut self, line: &[u8]) {
        let text = self.redact(&String::from_utf8_lossy(line));
        for f in &mut self.files {
            let _ = f.write_all(text.as_bytes());
        }
    }
}

impl Drop for Tee {
    fn drop(&mut self) {
        // (what was left without its line break: written at the end)
        if !self.line.is_empty() {
            let rest = std::mem::take(&mut self.line);
            self.write_line(&rest);
        }
    }
}

impl Write for Tee {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        if let Err(error) = self.out.write_all(b) {
            if error.kind() != std::io::ErrorKind::BrokenPipe {
                return Err(error);
            }
            // The launcher closed its pipe. Finish writing the local build logs.
            self.out = Box::new(std::io::sink());
        }
        self.line.extend_from_slice(b);
        while let Some(i) = self.line.iter().position(|&c| c == b'\n') {
            let line: Vec<u8> = self.line.drain(..=i).collect();
            self.write_line(&line);
        }
        Ok(b.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        for f in &mut self.files {
            let _ = f.flush();
        }
        self.out.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_disconnected_launcher_does_not_interrupt_the_saved_build_log() {
        struct Closed;
        impl Write for Closed {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let dir = crate::scratch("log-pipe");
        let file = dir.join("build.log");
        let mut tee = Tee::new(Box::new(Closed), &[&file], |error| panic!("{error}"));
        writeln!(tee, "first").unwrap();
        writeln!(tee, "second").unwrap();
        drop(tee);
        assert_eq!(std::fs::read_to_string(file).unwrap(), "first\nsecond\n");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn whole_lines_without_the_home_folder() {
        let d = crate::scratch("log");
        let f = d.join("build.log");
        let mut t = Tee::new(Box::new(std::io::sink()), &[&f], |error| panic!("{error}"));
        t.who = Who { home: "/home/player".into(), user: "player".into(), folder: String::new(), windows: false };
        write!(t, "reading /home/player/Games/x.nup").unwrap();
        writeln!(t, " now, from /media/player/usb").unwrap();
        write!(t, "half").unwrap();
        t.flush().unwrap();
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "reading ~/Games/x.nup now, from /media/<user>/usb\n");
        drop(t);
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "reading ~/Games/x.nup now, from /media/<user>/usb\nhalf");
        let _ = std::fs::remove_dir_all(&d);
    }
}
