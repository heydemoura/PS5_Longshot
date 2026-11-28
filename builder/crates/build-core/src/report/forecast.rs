use super::{Event, Stage};
use std::time::Instant;

const ETA_WARMUP: f64 = 10.0;

struct Work {
    stage: Stage,
    seconds: f64,
}

pub(super) struct Forecast {
    plan: Vec<Work>,
    active: Option<usize>,
    began: Instant,
    fraction: f64,
    // A sample taken after progress begins. This also handles shader progress buffered while compiling.
    sample: Option<(Instant, f64)>,
    measured_rate: bool,
    compiler_eta: Option<(Instant, u64)>,
    parallel_shaders: Option<super::ShaderProgress>,
    shaders_cached: bool,
    last: (u64, Option<u64>),
}

impl Forecast {
    pub(super) fn new(estimates: Vec<(Stage, f64)>, now: Instant, shaders_cached: bool) -> (Self, Event) {
        let forecast = Self {
            plan: estimates.into_iter().map(|(stage, seconds)| Work { stage, seconds }).collect(),
            active: None,
            began: now,
            fraction: 0.0,
            sample: None,
            measured_rate: false,
            compiler_eta: None,
            parallel_shaders: None,
            shaders_cached,
            last: (0, None),
        };
        (forecast, Event::BuildProgress { fraction: 0.0, seconds: None })
    }

    fn enter(&mut self, stage: Stage) {
        let next = self.plan.iter().position(|work| work.stage == stage);
        if self.active == next || next.is_none() {
            return;
        }
        self.active = next;
        self.fraction = if stage == Stage::Shaders {
            self.parallel_shaders.as_ref().and_then(|progress| progress()).unwrap_or(0.0)
        } else {
            0.0
        };
        self.sample = None;
        self.measured_rate = false;
        self.compiler_eta = None;
    }

    pub(super) fn parallel_shaders(&mut self, progress: super::ShaderProgress) {
        self.parallel_shaders = Some(progress);
        self.shaders_cached = false;
    }

    pub(super) fn shaders_cached(&mut self, cached: bool) {
        self.shaders_cached = cached;
        self.parallel_shaders = None;
        if !cached && self.active.is_some_and(|i| self.plan[i].stage == Stage::Shaders) {
            // The early worker can finish but fail to publish its output. The fallback starts from zero.
            self.fraction = 0.0;
            self.sample = None;
            self.measured_rate = false;
        }
    }

    pub(super) fn observe(&mut self, event: &Event, now: Instant) -> Option<Event> {
        match *event {
            Event::Stage { stage, .. } => self.enter(stage),
            Event::Progress { stage, fraction, .. } => {
                let i = self.active?;
                if self.plan[i].stage != stage {
                    return None;
                }
                if let Some((at, previous)) = self.sample {
                    let elapsed = now.duration_since(at).as_secs_f64();
                    let change = fraction - previous;
                    if fraction < previous {
                        self.sample = Some((now, fraction));
                        self.measured_rate = false;
                    } else if elapsed >= 5.0 && change >= 0.05 && fraction < 1.0 && stage != Stage::Compile {
                        let seconds = &mut self.plan[i].seconds;
                        // Blend with the existing estimate so one uneven part doesn't set the pace for all of it.
                        *seconds = (*seconds + (elapsed / change).clamp(*seconds / 4.0, *seconds * 4.0)) / 2.0;
                        self.sample = Some((now, fraction));
                        self.measured_rate = true;
                    }
                } else {
                    self.sample = Some((now, fraction));
                }
                self.fraction = fraction;
            }
            Event::CompilePlan { changed, total } if total > 0 => {
                let work = self.plan.get_mut(self.active?)?;
                if work.stage != Stage::Compile {
                    return None;
                }
                // Object-cache hits still have to be checked and the game linked.
                work.seconds *= (changed as f64 / total as f64).clamp(0.03, 1.0);
            }
            Event::Eta { seconds } => {
                if self.plan[self.active?].stage != Stage::Compile {
                    return None;
                }
                self.compiler_eta = Some((now, seconds));
            }
            Event::Done { .. } => {
                // The play command still has to spawn the game. The launcher receives GameStarted then.
                if self.plan.last().is_some_and(|work| work.stage == Stage::Start) {
                    self.enter(Stage::Start);
                } else {
                    return Some(Event::BuildProgress { fraction: 1.0, seconds: Some(0) });
                }
            }
            _ => return None,
        }
        self.snapshot(now)
    }

    fn remaining_active(&self, now: Instant) -> Option<f64> {
        let i = self.active?;
        if self.plan[i].stage == Stage::Shaders && self.shaders_cached {
            return Some(0.0);
        }
        let weight = self.plan[i].seconds;
        if let Some((at, seconds)) = self.compiler_eta {
            let link = weight.clamp(2.0, 10.0);
            Some((seconds as f64 - now.duration_since(at).as_secs_f64()).max(0.0) + link)
        } else {
            Some(weight * (1.0 - self.fraction))
        }
    }

    fn snapshot(&mut self, now: Instant) -> Option<Event> {
        let i = self.active?;
        let elapsed = now.duration_since(self.began).as_secs_f64();
        let mut before_shaders = self.remaining_active(now)?;
        let mut start = 0.0;
        let mut shaders = 0.0;
        for work in &self.plan[i + 1..] {
            match work.stage {
                Stage::Start => start = work.seconds,
                Stage::Shaders if self.shaders_cached => (),
                Stage::Shaders => match self.parallel_shaders.as_ref().and_then(|progress| progress()) {
                    Some(fraction) => shaders = work.seconds * (1.0 - fraction),
                    None => before_shaders += work.seconds,
                },
                _ => before_shaders += work.seconds,
            }
        }
        let left = (before_shaders.max(shaders) + start).max(1.0);
        // Cached stages can take a fraction of their initial budget. Use real elapsed time so the percentage and ETA
        // describe the same operation, and time spent on overlapping work counts once.
        let fraction = (elapsed / (elapsed + left)).min(0.99);
        let known = match self.plan[i].stage {
            Stage::Compile => self.compiler_eta.is_some(),
            Stage::Finish | Stage::Start => true,
            Stage::Shaders if self.shaders_cached => true,
            _ => self.measured_rate,
        };
        let seconds = (elapsed >= ETA_WARMUP && known).then(|| left.ceil() as u64);
        let key = ((fraction * 1000.0).round() as u64, seconds);
        if self.last == key {
            return None;
        }
        self.last = key;
        Some(Event::BuildProgress { fraction, seconds })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn stage(stage: Stage) -> Event {
        Event::Stage { stage, text: String::new() }
    }

    fn progress(stage: Stage, fraction: f64) -> Event {
        Event::Progress { stage, fraction, overall: 1.0 }
    }

    fn done() -> Event {
        Event::Done { folder: String::new(), seconds: 0.0, compile_seconds: 0.0, previous_seconds: None }
    }

    fn total(event: Option<Event>) -> (f64, Option<u64>) {
        match event {
            Some(Event::BuildProgress { fraction, seconds }) => (fraction, seconds),
            _ => panic!("expected overall build progress"),
        }
    }

    #[test]
    fn faster_cached_stages_do_not_earn_unspent_time() {
        let start = Instant::now();
        let (mut forecast, _) = Forecast::new(vec![(Stage::Read, 200.0), (Stage::Shaders, 100.0)], start, false);
        forecast.observe(&stage(Stage::Read), start + Duration::from_secs(1));
        let shaders = start + Duration::from_secs(2);
        let (fraction, seconds) = total(forecast.observe(&stage(Stage::Shaders), shaders));
        assert_eq!(seconds, None);
        assert!((fraction - 2.0 / 102.0).abs() < 1e-9);

        forecast.observe(&progress(Stage::Shaders, 0.0), shaders);
        let (fraction, seconds) =
            total(forecast.observe(&progress(Stage::Shaders, 0.1), start + Duration::from_secs(12)));
        assert_eq!(seconds, Some(90));
        assert!((fraction - 12.0 / 102.0).abs() < 1e-9);
    }

    #[test]
    fn forecast_includes_shaders_and_game_start_in_the_whole_eta() {
        let now = Instant::now();
        let (mut f, announced) =
            Forecast::new(vec![(Stage::Compile, 100.0), (Stage::Shaders, 80.0), (Stage::Start, 2.0)], now, false);
        assert_eq!(total(Some(announced)), (0.0, None));
        f.observe(&stage(Stage::Compile), now);
        assert!(f.observe(&Event::CompilePlan { changed: 25, total: 100 }, now).is_none());
        let (compiled, seconds) = total(f.observe(&progress(Stage::Compile, 0.5), now + Duration::from_secs(12)));
        assert!(compiled < 0.25);
        assert_eq!(seconds, None);
        assert_eq!(total(f.observe(&Event::Eta { seconds: 6 }, now + Duration::from_secs(12))).1, Some(98));

        let (shader_start, seconds) = total(f.observe(&stage(Stage::Shaders), now + Duration::from_secs(21)));
        assert!(shader_start < 0.5);
        assert_eq!(seconds, None);
        f.observe(&progress(Stage::Shaders, 0.0), now + Duration::from_secs(21));
        let (shader_half, seconds) = total(f.observe(&progress(Stage::Shaders, 0.5), now + Duration::from_secs(61)));
        assert!(shader_start < shader_half && shader_half < 0.99);
        assert_eq!(seconds, Some(42));
        let (handoff, seconds) = total(f.observe(&done(), now + Duration::from_secs(103)));
        assert!(shader_half < handoff && handoff < 1.0);
        assert_eq!(seconds, Some(2));
    }

    #[test]
    fn forecast_counts_shader_work_running_alongside_the_compile() {
        let now = Instant::now();
        let (mut f, announced) = Forecast::new(vec![(Stage::Compile, 100.0), (Stage::Shaders, 80.0)], now, false);
        assert_eq!(total(Some(announced)), (0.0, None));
        f.observe(&stage(Stage::Compile), now);
        let progress_at = now + Duration::from_secs(10);
        let (serial, seconds) = total(f.observe(&Event::Eta { seconds: 50 }, progress_at));
        assert_eq!(seconds, Some(140));
        f.parallel_shaders(Box::new(|| Some(0.5)));
        let (parallel, seconds) = total(f.observe(&progress(Stage::Compile, 0.5), progress_at));
        assert!(parallel > serial);
        assert_eq!(seconds, Some(60));
        assert_eq!(total(f.observe(&done(), progress_at)), (1.0, Some(0)));
    }

    #[test]
    fn rebuild_only_restores_the_shader_budget_when_preparation_is_needed() {
        let now = Instant::now();
        for cached_after_build in [true, false] {
            let (mut f, announced) =
                Forecast::new(vec![(Stage::Compile, 30.0), (Stage::Shaders, 311.0), (Stage::Start, 15.0)], now, true);
            assert_eq!(total(Some(announced)), (0.0, None));
            f.observe(&stage(Stage::Compile), now);
            assert_eq!(total(f.observe(&Event::Eta { seconds: 4 }, now + Duration::from_secs(5))).1, None);
            assert_eq!(total(f.observe(&Event::Eta { seconds: 4 }, now + Duration::from_secs(10))).1, Some(29));
            f.shaders_cached(cached_after_build);
            let (fraction, seconds) = total(f.observe(&stage(Stage::Shaders), now + Duration::from_secs(11)));
            let budget = if cached_after_build { 15.0 } else { 326.0 };
            assert!((fraction - 11.0 / (11.0 + budget)).abs() < 1e-9);
            assert_eq!(seconds, cached_after_build.then_some(15));
        }
    }
}
