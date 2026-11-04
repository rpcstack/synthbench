//! Policy-liveness probe.
//!
//! Proves that real RPCs go through Masa's hooks in the frontend and child
//! services, with a check that cannot pass in baseline mode:
//!
//! 1. Control: `--control` requests through the frontend with a generous SLO
//!    must all succeed (in both modes), so the probe is not just rejecting
//!    everything.
//! 2. Expired deadline: for the frontend and each `--target`, send one request
//!    whose Masa context (the `ctx` header written by `RootContext::attach`) has
//!    an end-to-end deadline already in the past. Masa's `abort_slo` deadline
//!    guard runs in the server hooks before the handler is polled and answers
//!    `DEADLINE_EXCEEDED "/EarlyReturn?src=<that service>::..."`. With no-op
//!    hooks the header is ignored, the handler runs, and no `/EarlyReturn` can be
//!    produced: that string only exists in Masa's policy modules.
//!
//! `--expect masa` requires an `/EarlyReturn` from every target;
//! `--expect baseline` requires none. Exit status 0 means the expectation held.

use std::time::Duration;

use structopt::StructOpt;
use synthbench::tonic::child::{child_client::ChildClient, MethodRequest};
use synthbench::tonic::frontend::{frontend_client::FrontendClient, ARequest};
use tonic::transport::Channel;
use tonic::{Request, Status};

use masa::RootContext;

#[derive(StructOpt, Debug)]
struct Args {
    /// `baseline` or `masa`.
    #[structopt(long)]
    expect: String,
    /// Frontend address, e.g. http://frontend:8000.
    #[structopt(long)]
    frontend: String,
    /// Child to probe directly, as `service_id::method=http://host:port`.
    #[structopt(long = "target")]
    targets: Vec<String>,
    /// Number of control requests (generous SLO) that must succeed.
    #[structopt(long, default_value = "20")]
    control: usize,
    /// Retry a request for up to this many seconds until the stack answers.
    #[structopt(long, default_value = "0")]
    wait_secs: u64,
    /// Per-request timeout in milliseconds.
    #[structopt(long, default_value = "5000")]
    timeout_ms: u64,
}

#[derive(Debug)]
enum Outcome {
    Ok,
    EarlyReturn(String),
    Error(#[allow(dead_code)] String),
}

impl Outcome {
    fn of<T>(r: Result<tonic::Response<T>, Status>) -> Self {
        match r {
            Ok(_) => Outcome::Ok,
            Err(s) if s.message().starts_with("/EarlyReturn") => {
                Outcome::EarlyReturn(s.message().to_string())
            }
            Err(s) => Outcome::Error(format!("{:?}: {}", s.code(), s.message())),
        }
    }
}

/// A Masa root context whose end-to-end deadline has already passed.
async fn expired<T>(api: &str, msg: T) -> Request<T> {
    let ctx = RootContext::from(masa::create_context(api, Duration::from_micros(1)));
    tokio::time::sleep(Duration::from_millis(2)).await;
    ctx.attach(Request::new(msg))
}

async fn channel(addr: &str, timeout: Duration) -> Result<Channel, Box<dyn std::error::Error>> {
    Ok(Channel::from_shared(addr.to_string())?
        .timeout(timeout)
        .connect_timeout(timeout)
        .connect()
        .await?)
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::from_args();
    let masa_mode = match args.expect.as_str() {
        "masa" => true,
        "baseline" => false,
        other => return Err(format!("--expect must be baseline or masa, got {other}").into()),
    };
    let timeout = Duration::from_millis(args.timeout_ms);
    let mut failures = Vec::new();

    // 0. Readiness: wait until the frontend answers an "a" request end to end.
    let deadline = std::time::Instant::now() + Duration::from_secs(args.wait_secs);
    let mut fe = loop {
        let attempt = async {
            let ch = channel(&args.frontend, timeout).await?;
            let mut fe = FrontendClient::new(ch);
            let ctx = RootContext::from(masa::create_context("a", Duration::from_secs(5)));
            fe.handle_a(ctx.attach(Request::new(ARequest {}))).await?;
            Ok::<_, Box<dyn std::error::Error>>(fe)
        };
        match attempt.await {
            Ok(fe) => break fe,
            Err(e) if std::time::Instant::now() < deadline => {
                println!("waiting for frontend: {e}");
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
            Err(e) => return Err(format!("frontend not ready: {e}").into()),
        }
    };

    // 1. Control: generous SLO, must succeed in both modes.
    let mut ok = 0;
    for _ in 0..args.control {
        let ctx = RootContext::from(masa::create_context("a", Duration::from_secs(5)));
        match fe.handle_a(ctx.attach(Request::new(ARequest {}))).await {
            Ok(_) => ok += 1,
            Err(s) => println!("control: error {:?}: {}", s.code(), s.message()),
        }
    }
    println!(
        "control: {ok}/{} requests with a 5 s SLO succeeded",
        args.control
    );
    if ok != args.control {
        failures.push(format!("control: only {ok}/{} succeeded", args.control));
    }

    // 2. Expired deadlines: the frontend, then each child directly.
    let mut names = vec!["frontend".to_string()];
    let mut outcomes = vec![Outcome::of(
        fe.handle_a(expired("a", ARequest {}).await).await,
    )];
    for t in &args.targets {
        let (target, addr) = t
            .split_once('=')
            .ok_or_else(|| format!("--target must be service::method=addr, got {t}"))?;
        let (service_id, method_name) = target
            .split_once("::")
            .ok_or_else(|| format!("--target must be service::method=addr, got {t}"))?;
        let mut c = ChildClient::new(channel(addr, timeout).await?);
        let msg = MethodRequest {
            service_id: service_id.to_string(),
            method_name: method_name.to_string(),
            sent_at: 0,
        };
        names.push(target.to_string());
        outcomes.push(Outcome::of(c.handle_method(expired("a", msg).await).await));
    }

    let mut early_returns = 0;
    for (name, outcome) in names.iter().zip(&outcomes) {
        println!("expired-deadline {name:<28} -> {outcome:?}");
        match (outcome, masa_mode) {
            (Outcome::EarlyReturn(_), true) => early_returns += 1,
            (Outcome::EarlyReturn(msg), false) => {
                early_returns += 1;
                failures.push(format!(
                    "{name}: baseline produced a Masa early return: {msg}"
                ));
            }
            (_, true) => failures.push(format!("{name}: expected /EarlyReturn, got {outcome:?}")),
            (_, false) => {}
        }
    }
    println!(
        "policy-live: {early_returns}/{} services rejected an expired Masa deadline (expect {})",
        names.len(),
        args.expect
    );

    if failures.is_empty() {
        println!("PROBE PASS ({})", args.expect);
        Ok(())
    } else {
        for f in &failures {
            println!("FAIL: {f}");
        }
        println!("PROBE FAIL ({})", args.expect);
        std::process::exit(1);
    }
}
