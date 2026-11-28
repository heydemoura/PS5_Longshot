//! Adapt tracing records to the builder's versioned diagnostic event.
use super::Event;
use serde_json::{Map, Value};
use tracing::field::{Field, Visit};
use tracing_subscriber::{Layer, layer::Context};

/// Send each diagnostic to the reporter or CLI sink that owns its output.
pub struct Diagnostics<F>(pub F);

impl<S, F> Layer<S> for Diagnostics<F>
where
    S: tracing::Subscriber,
    F: Fn(Event) + Send + Sync + 'static,
{
    fn on_event(&self, event: &tracing::Event<'_>, _: Context<'_, S>) {
        let mut fields = Fields(Map::new());
        event.record(&mut fields);
        let mut string = |key| fields.0.remove(key).and_then(|value| value.as_str().map(str::to_owned));
        (self.0)(Event::Log {
            text: string("message").unwrap_or_default(),
            source: string("source").unwrap_or_else(|| "builder".into()),
            stream: string("stream"),
            code: string("code"),
            level: event.metadata().level().as_str(),
            target: event.metadata().target().to_owned(),
            fields: fields.0,
        });
    }
}

struct Fields(Map<String, Value>);

impl Visit for Fields {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.0.insert(field.name().into(), value.into());
    }
    fn record_u64(&mut self, field: &Field, value: u64) {
        self.0.insert(field.name().into(), value.into());
    }
    fn record_i64(&mut self, field: &Field, value: i64) {
        self.0.insert(field.name().into(), value.into());
    }
    fn record_bool(&mut self, field: &Field, value: bool) {
        self.0.insert(field.name().into(), value.into());
    }
    fn record_f64(&mut self, field: &Field, value: f64) {
        self.0.insert(field.name().into(), value.into());
    }
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        self.record_str(field, &format!("{value:?}"));
    }
}
