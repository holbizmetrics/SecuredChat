using System.Diagnostics;
using System.Text;
using SecuredChat.Bus;
using SecuredChat.Terminal;

// ---------------------------------------------------------------------------
// The deterministic terminal, first cut. Two modes:
//   repl                                     interactive prompt
//   agent --bus <repo> [--room r] [--identity id] [--poll s] [--workdir d]
//                                            live on the bus, answer "cmd"s
// The command set is the allowlist: what is not registered cannot be invoked,
// locally or over the bus. Verbs below are BCL+git only (this container has no
// NuGet access); Roslyn workspace / scripting verbs land as the next slice on
// a machine with package feeds.
// ---------------------------------------------------------------------------

const string Version = "0.1.0";

string workdir = Directory.GetCurrentDirectory();
var host = new CommandHost();

// ----- git plumbing for the verbs ------------------------------------------
(int Code, string Out, string Err) Git(params string[] a)
{
	var psi = new ProcessStartInfo("git") { WorkingDirectory = workdir, RedirectStandardOutput = true, RedirectStandardError = true };
	foreach (var x in a) psi.ArgumentList.Add(x);
	using var p = Process.Start(psi)!;
	string o = p.StandardOutput.ReadToEnd(); string e = p.StandardError.ReadToEnd();
	p.WaitForExit();
	return (p.ExitCode, o, e);
}

string SafePath(string relative)
{
	string full = Path.GetFullPath(Path.Combine(workdir, relative));
	string root = Path.GetFullPath(workdir) + Path.DirectorySeparatorChar;
	if (!full.StartsWith(root, StringComparison.Ordinal) && full != Path.GetFullPath(workdir))
		throw new UnauthorizedAccessException($"path escapes the workdir: {relative}");
	return full;
}

// ----- the registered verbs (= the standing permissions) --------------------
host.Register("ping", "liveness check; echoes 'pong' + args",
	(argv, _) => argv.Count == 0 ? "pong" : $"pong {string.Join(' ', argv)}");

host.Register("version", "terminal version + runtime",
	(_, _) => $"cs-terminal {Version} (.NET {Environment.Version}, {Environment.OSVersion.Platform})");

host.Register("help", "list registered commands",
	(_, _) => string.Join('\n', host.Commands.Select(c => $"{c.Name,-12} {c.Help}")));

host.Register("echo", "echo raw arguments back",
	(_, raw) => raw);

host.Register("pwd", "the terminal's working directory",
	(_, _) => workdir);

host.Register("ls", "list a directory under the workdir (default: .)",
	(argv, _) =>
	{
		string dir = SafePath(argv.Count > 0 ? argv[0] : ".");
		if (!Directory.Exists(dir)) return $"error: no such directory: {argv.FirstOrDefault() ?? "."}";
		var entries = Directory.GetDirectories(dir).Select(d => Path.GetFileName(d) + "/")
			.Concat(Directory.GetFiles(dir).Select(Path.GetFileName))
			.OrderBy(n => n, StringComparer.Ordinal);
		return string.Join('\n', entries!);
	});

host.Register("read", "print a file under the workdir (first 200 lines)",
	(argv, _) =>
	{
		if (argv.Count == 0) return "usage: read <relative-path>";
		string file = SafePath(argv[0]);
		if (!File.Exists(file)) return $"error: no such file: {argv[0]}";
		var lines = File.ReadLines(file).Take(200).ToList();
		return string.Join('\n', lines) + (lines.Count == 200 ? "\n… (truncated at 200 lines)" : "");
	});

host.Register("hash", "sha256 of a file under the workdir",
	(argv, _) =>
	{
		if (argv.Count == 0) return "usage: hash <relative-path>";
		string file = SafePath(argv[0]);
		if (!File.Exists(file)) return $"error: no such file: {argv[0]}";
		using var sha = System.Security.Cryptography.SHA256.Create();
		using var s = File.OpenRead(file);
		return Convert.ToHexString(sha.ComputeHash(s)).ToLowerInvariant() + "  " + argv[0];
	});

host.Register("git-status", "git status --short of the workdir",
	(_, _) => { var r = Git("status", "--short", "--branch"); return r.Code == 0 ? (r.Out.Trim().Length > 0 ? r.Out.TrimEnd() : "clean") : $"error: {r.Err.Trim()}"; });

host.Register("git-log", "last N commits (default 5), one line each",
	(argv, _) =>
	{
		string n = argv.Count > 0 && int.TryParse(argv[0], out var k) && k is > 0 and <= 50 ? k.ToString() : "5";
		var r = Git("log", $"-{n}", "--oneline", "--no-decorate");
		return r.Code == 0 ? r.Out.TrimEnd() : $"error: {r.Err.Trim()}";
	});

host.Register("verify", "run `git fsck` + working-tree diff stat — a mechanical health check",
	(_, _) =>
	{
		var fsck = Git("fsck", "--no-progress");
		var diff = Git("diff", "--stat");
		var sb = new StringBuilder();
		sb.AppendLine(fsck.Code == 0 ? "fsck: ok" : $"fsck: FAILED — {fsck.Err.Trim()}");
		sb.Append(diff.Out.Trim().Length > 0 ? $"uncommitted changes:\n{diff.Out.TrimEnd()}" : "working tree: clean");
		return sb.ToString();
	});

// ----- modes -----------------------------------------------------------------
if (args.Length == 0 || args[0] == "repl")
{
	Console.WriteLine($"cs-terminal {Version} — deterministic core. 'help' lists verbs, 'exit' quits.");
	while (true)
	{
		Console.Write("> ");
		string? line = Console.ReadLine();
		if (line is null || line.Trim() is "exit" or "quit") break;
		if (line.Trim().Length == 0) continue;
		Console.WriteLine(host.Run(line));
	}
	return 0;
}

if (args[0] == "once")
{
	// one command, stdout the result — the scriptable path
	Console.WriteLine(host.Run(string.Join(' ', args.Skip(1))));
	return 0;
}

if (args[0] == "agent")
{
	string? busPath = null, room = "relay", identity = $"cs-terminal-{Environment.MachineName.ToLowerInvariant()}";
	double poll = 3.0;
	for (int i = 1; i < args.Length - 1; i++)
		switch (args[i])
		{
			case "--bus": busPath = args[++i]; break;
			case "--room": room = args[++i]; break;
			case "--identity": identity = args[++i]; break;
			case "--poll": poll = double.Parse(args[++i], System.Globalization.CultureInfo.InvariantCulture); break;
			case "--workdir": workdir = Path.GetFullPath(args[++i]); break;
		}
	if (busPath is null) { Console.Error.WriteLine("usage: agent --bus <repo> [--room r] [--identity id] [--poll s] [--workdir d]"); return 2; }

	var bus = new GitBusTransport(busPath, room, identity);
	var agent = new BusAgent(bus, host);
	using var cts = new CancellationTokenSource();
	Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };
	agent.RunLoop(poll, cts.Token);
	return 0;
}

Console.Error.WriteLine("usage: cs-terminal [repl | once <command...> | agent --bus <repo> ...]");
return 2;
