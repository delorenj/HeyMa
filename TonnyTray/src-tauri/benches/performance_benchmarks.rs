/// Performance benchmarks for TonnyTray
use criterion::{black_box, criterion_group, criterion_main, Criterion, BenchmarkId};
use std::time::Duration;
use tonnytray::state::*;
use tokio::runtime::Runtime;

/// Benchmark state creation and access
fn bench_state_operations(c: &mut Criterion) {
    let mut group = c.benchmark_group("state_operations");

    group.bench_function("create_state", |b| {
        b.iter(|| {
            let settings = AppSettings::default();
            black_box(create_state(settings))
        })
    });

    group.bench_function("lock_and_read", |b| {
        let settings = AppSettings::default();
        let state = create_state(settings);

        b.iter(|| {
            let locked = state.lock().unwrap();
            black_box(&locked.recording)
        })
    });

    group.bench_function("lock_and_write", |b| {
        let settings = AppSettings::default();
        let state = create_state(settings);

        b.iter(|| {
            let mut locked = state.lock().unwrap();
            locked.recording = !locked.recording;
        })
    });

    group.bench_function("add_transcription", |b| {
        let settings = AppSettings::default();
        let state = create_state(settings);

        b.iter(|| {
            let mut locked = state.lock().unwrap();
            locked.add_transcription(
                black_box("Test transcription".to_string()),
                true,
                None,
            )
        })
    });

    group.finish();
}

/// Benchmark transcription history operations
fn bench_transcription_history(c: &mut Criterion) {
    let mut group = c.benchmark_group("transcription_history");

    // Benchmark with different history sizes
    for size in [10, 50, 100].iter() {
        group.bench_with_input(BenchmarkId::from_parameter(size), size, |b, &size| {
            let settings = AppSettings::default();
            let state = create_state(settings);

            // Pre-populate history
            {
                let mut locked = state.lock().unwrap();
                for i in 0..size {
                    locked.add_transcription(
                        format!("Transcription {}", i),
                        true,
                        None,
                    );
                }
            }

            b.iter(|| {
                let mut locked = state.lock().unwrap();
                locked.add_transcription(
                    black_box("New transcription".to_string()),
                    true,
                    None,
                )
            })
        });
    }

    group.finish();
}

/// Benchmark configuration operations
fn bench_config_operations(c: &mut Criterion) {
    use tonnytray::config::*;
    use tempfile::tempdir;

    let mut group = c.benchmark_group("config_operations");

    group.bench_function("create_default", |b| {
        b.iter(|| black_box(Config::default()))
    });

    group.bench_function("to_app_settings", |b| {
        let config = Config::default();
        b.iter(|| black_box(config.to_app_settings()))
    });

    group.bench_function("save_and_load", |b| {
        let dir = tempdir().unwrap();
        let config_path = dir.path().join("bench_config.toml");
        let config = Config::default();

        b.iter(|| {
            config.save(&config_path).unwrap();
            black_box(Config::load(&config_path).unwrap())
        })
    });

    group.finish();
}

/// Benchmark database operations
fn bench_database_operations(c: &mut Criterion) {
    use tonnytray::database::*;
    use tempfile::tempdir;
    use chrono::Utc;

    let mut group = c.benchmark_group("database_operations");

    group.bench_function("create_database", |b| {
        b.iter(|| {
            let dir = tempdir().unwrap();
            let db_path = dir.path().join("bench.db");
            black_box(AppDatabase::new(db_path).unwrap())
        })
    });

    group.bench_function("insert_log", |b| {
        let dir = tempdir().unwrap();
        let db = AppDatabase::new(dir.path().join("logs.db")).unwrap();

        let log = LogEntry {
            id: None,
            timestamp: Utc::now(),
            level: "INFO".to_string(),
            component: "benchmark".to_string(),
            message: "Test log message".to_string(),
            metadata: None,
        };

        b.iter(|| {
            black_box(db.insert_log(&log).unwrap())
        })
    });

    group.bench_function("query_logs", |b| {
        let dir = tempdir().unwrap();
        let db = AppDatabase::new(dir.path().join("query_logs.db")).unwrap();

        // Pre-populate with logs
        for i in 0..1000 {
            let log = LogEntry {
                id: None,
                timestamp: Utc::now(),
                level: "INFO".to_string(),
                component: "benchmark".to_string(),
                message: format!("Log {}", i),
                metadata: None,
            };
            db.insert_log(&log).unwrap();
        }

        b.iter(|| {
            black_box(db.get_logs(50, 0).unwrap())
        })
    });

    group.bench_function("insert_transcription", |b| {
        let dir = tempdir().unwrap();
        let db = AppDatabase::new(dir.path().join("trans.db")).unwrap();

        let trans = TranscriptionEntry {
            timestamp: Utc::now(),
            text: "Benchmark transcription".to_string(),
            success: true,
            response: None,
        };

        b.iter(|| {
            black_box(db.insert_transcription(&trans).unwrap())
        })
    });

    group.finish();
}

/// Benchmark concurrent state access
fn bench_concurrent_access(c: &mut Criterion) {
    let rt = Runtime::new().unwrap();

    let mut group = c.benchmark_group("concurrent_access");
    group.measurement_time(Duration::from_secs(10));

    group.bench_function("parallel_reads", |b| {
        let settings = AppSettings::default();
        let state = create_state(settings);

        b.iter(|| {
            rt.block_on(async {
                let handles: Vec<_> = (0..10)
                    .map(|_| {
                        let state_clone = state.clone();
                        tokio::spawn(async move {
                            let locked = state_clone.lock().unwrap();
                            black_box(&locked.recording);
                        })
                    })
                    .collect();

                for handle in handles {
                    handle.await.unwrap();
                }
            })
        })
    });

    group.bench_function("parallel_writes", |b| {
        let settings = AppSettings::default();
        let state = create_state(settings);

        b.iter(|| {
            rt.block_on(async {
                let handles: Vec<_> = (0..10)
                    .map(|i| {
                        let state_clone = state.clone();
                        tokio::spawn(async move {
                            let mut locked = state_clone.lock().unwrap();
                            locked.add_transcription(
                                format!("Trans {}", i),
                                true,
                                None,
                            );
                        })
                    })
                    .collect();

                for handle in handles {
                    handle.await.unwrap();
                }
            })
        })
    });

    group.finish();
}

/// Benchmark serialization/deserialization
fn bench_serialization(c: &mut Criterion) {
    let mut group = c.benchmark_group("serialization");

    let settings = AppSettings::default();
    let json = serde_json::to_string(&settings).unwrap();

    group.bench_function("serialize_settings", |b| {
        b.iter(|| {
            black_box(serde_json::to_string(&settings).unwrap())
        })
    });

    group.bench_function("deserialize_settings", |b| {
        b.iter(|| {
            black_box(serde_json::from_str::<AppSettings>(&json).unwrap())
        })
    });

    group.bench_function("serialize_transcription", |b| {
        use chrono::Utc;
        let entry = TranscriptionEntry {
            timestamp: Utc::now(),
            text: "Test transcription text".to_string(),
            success: true,
            response: Some("Response text".to_string()),
        };

        b.iter(|| {
            black_box(serde_json::to_string(&entry).unwrap())
        })
    });

    group.finish();
}

criterion_group!(
    benches,
    bench_state_operations,
    bench_transcription_history,
    bench_config_operations,
    bench_database_operations,
    bench_concurrent_access,
    bench_serialization,
);

criterion_main!(benches);
