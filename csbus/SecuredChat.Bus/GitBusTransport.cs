using System.Diagnostics;
using System.Text.RegularExpressions;

namespace SecuredChat.Bus;

/// <summary>
/// Append-only JSONL chat log in a git repo. Push on send, pull on recv.
/// A faithful C# peer of SecuredChat's cli/transport.py GitBusTransport:
/// same layout (&lt;room&gt;/chat.jsonl + archive/ + presence/), same bus marker
/// (.securedchat-bus), same advisory .send.lock, same union-merge
/// .gitattributes, same pull→append→commit→push-with-rebase-retry send.
/// </summary>
public sealed class GitBusTransport
{
	public const string BusMarker = ".securedchat-bus";
	private static readonly Regex SafeName = new("^[A-Za-z0-9._-]+$", RegexOptions.Compiled);

	public string BusRepo { get; }
	public string Room { get; }
	public string Identity { get; }
	/// <summary>False after a failed sync — lets callers tell "0 pending" apart
	/// from "offline / stale", mirroring transport.py's last_pull_ok.</summary>
	public bool LastPullOk { get; private set; } = true;

	private string RoomDir => Path.Combine(BusRepo, Room);
	private string ChatFile => Path.Combine(RoomDir, "chat.jsonl");
	private string ArchiveDir => Path.Combine(RoomDir, "archive");
	private string LockFile => Path.Combine(RoomDir, ".send.lock");

	public GitBusTransport(string busRepo, string room, string identity)
	{
		if (!SafeName.IsMatch(room ?? ""))
			throw new ArgumentException($"invalid room '{room}': use only letters, digits, . _ -");
		if (!SafeName.IsMatch(identity ?? ""))
			throw new ArgumentException($"invalid identity '{identity}': use only letters, digits, . _ -");
		BusRepo = Path.GetFullPath(busRepo);
		Room = room!;
		Identity = identity!;
		if (!Directory.Exists(Path.Combine(BusRepo, ".git")))
			throw new InvalidOperationException($"Not a git repo: {BusRepo}");
		if (!File.Exists(Path.Combine(BusRepo, BusMarker)))
			Console.Error.WriteLine(
				$"securedchat: warning — {BusRepo} has no {BusMarker} marker; " +
				"a dedicated bus repo is expected (never point --bus at a code repo). " +
				"Run Init() to create it.");
		Directory.CreateDirectory(RoomDir);
	}

	// ----- git plumbing ---------------------------------------------------- //

	private (int Code, string StdOut, string StdErr) Git(params string[] args)
	{
		var psi = new ProcessStartInfo("git")
		{
			WorkingDirectory = BusRepo,
			RedirectStandardOutput = true,
			RedirectStandardError = true,
			UseShellExecute = false,
		};
		foreach (var a in args) psi.ArgumentList.Add(a);
		using var p = Process.Start(psi)!;
		string stdout = p.StandardOutput.ReadToEnd();
		string stderr = p.StandardError.ReadToEnd();
		p.WaitForExit();
		return (p.ExitCode, stdout, stderr);
	}

	private bool HasRemote() => Git("remote").StdOut.Trim().Length > 0;

	/// <summary>Ensure union-merge rules for the append-only chat logs. Without
	/// them, concurrent appends from two devices produce an add/add conflict that
	/// wedges pull --rebase. Returns true if it wrote the file (caller stages it).</summary>
	private bool EnsureGitAttributes()
	{
		string ga = Path.Combine(BusRepo, ".gitattributes");
		string[] rules = ["chat.jsonl merge=union", "chat-*.jsonl merge=union"];
		string existing = File.Exists(ga) ? File.ReadAllText(ga) : "";
		var missing = rules.Where(r => !existing.Contains(r)).ToArray();
		if (missing.Length == 0) return false;
		string prefix = existing.Length == 0
			? "# SecuredChat bus — chat logs are append-only JSONL; union-merge\n" +
			  "# so concurrent appends from different devices never conflict.\n"
			: existing.EndsWith('\n') ? "" : "\n";
		File.AppendAllText(ga, prefix + string.Join("\n", missing) + "\n");
		return true;
	}

	/// <summary>pull --rebase --autostash, upstream-pinned; on failure abort any
	/// half-finished rebase (unwedge) and warn loudly rather than silently serve
	/// stale state. Returns true on success. Public: the interop tests use it to
	/// stage a deliberate concurrent-push race.</summary>
	public bool PullRebase()
	{
		var up = Git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}");
		(int code, string stdout, string err) result;
		if (up.Code == 0 && up.StdOut.Trim().Contains('/'))
		{
			var parts = up.StdOut.Trim().Split('/', 2);
			var r = Git("pull", "--rebase", "--autostash", parts[0], parts[1]);
			result = (r.Code, r.StdOut, r.StdErr);
		}
		else
		{
			var r = Git("pull", "--rebase", "--autostash");
			result = (r.Code, r.StdOut, r.StdErr);
		}
		if (result.code == 0) { LastPullOk = true; return true; }
		string gitDir = Path.Combine(BusRepo, ".git");
		if (Directory.Exists(Path.Combine(gitDir, "rebase-merge")) ||
			Directory.Exists(Path.Combine(gitDir, "rebase-apply")))
			Git("rebase", "--abort");
		string msg = (result.err ?? "").Trim().Replace('\n', ' ');
		Console.Error.WriteLine(
			$"securedchat: WARNING pull --rebase failed ({msg[..Math.Min(200, msg.Length)]}); " +
			"local state may be stale (offline or merge conflict).");
		LastPullOk = false;
		return false;
	}

	// ----- init ------------------------------------------------------------ //

	/// <summary>Create bus marker + room chat.jsonl (+ union-merge .gitattributes)
	/// and commit them. Idempotent. Returns a human-readable status line.</summary>
	public string Init()
	{
		var toAdd = new List<string>();
		string marker = Path.Combine(BusRepo, BusMarker);
		if (!File.Exists(marker))
		{
			File.WriteAllText(marker, "securedchat bus repo — agent-to-agent chat only, never store code here\n");
			toAdd.Add(BusMarker);
		}
		if (EnsureGitAttributes()) toAdd.Add(".gitattributes");
		string? already = null;
		if (File.Exists(ChatFile))
			already = $"room already initialized: {ChatFile}";
		else
		{
			File.WriteAllText(ChatFile, "");
			toAdd.Add(Path.GetRelativePath(BusRepo, ChatFile));
		}
		if (toAdd.Count == 0) return already ?? $"room already initialized: {ChatFile}";
		foreach (var rel in toAdd) Git("add", rel);
		Git("commit", "-m", $"chat: init room {Room}");
		string tail = HasRemote()
			? "\n(remember to `git push` from the bus repo to publish)"
			: "\n(local bus, no remote — nothing to push)";
		return $"initialized: {string.Join(", ", toAdd)}{tail}";
	}

	// ----- send ------------------------------------------------------------ //

	public void Send(Message msg)
	{
		using var _ = AcquireSendLock();
		if (HasRemote()) PullRebase();
		AppendAndCommit(msg);
		if (!HasRemote())
		{
			// "sent" must not silently imply delivery: with no remote the message
			// is committed LOCALLY ONLY and can reach no peer. Warn loudly.
			Console.Error.WriteLine(
				$"securedchat: WARNING message {msg.Id[..Math.Min(8, msg.Id.Length)]} committed LOCALLY " +
				"ONLY — no remote configured, NOT published to any peer.");
			return;
		}
		PushWithRetry();
	}

	/// <summary>Append one message and commit it, authored as this identity.
	/// Internal step of Send, public so the interop tests can stage a
	/// concurrent-push race between commit and push.</summary>
	public void AppendAndCommit(Message msg)
	{
		bool gaAdded = EnsureGitAttributes();
		File.AppendAllText(ChatFile, msg.ToJsonl() + "\n");
		string rel = Path.GetRelativePath(BusRepo, ChatFile);
		var add = Git("add", rel);
		if (add.Code != 0) throw new InvalidOperationException($"git add failed: {add.StdErr.Trim()}");
		if (gaAdded) Git("add", ".gitattributes");
		var commit = Git(
			"-c", $"user.email={Identity}@securedchat-cli",
			"-c", $"user.name={Identity}",
			"commit", "-m", $"chat: {Room} {msg.Id[..Math.Min(8, msg.Id.Length)]}");
		if (commit.Code != 0)
			throw new InvalidOperationException(
				$"git commit failed: {(commit.StdErr.Trim().Length > 0 ? commit.StdErr.Trim() : commit.StdOut.Trim())}");
	}

	/// <summary>Push, retrying up to 3 times with pull --rebase between attempts
	/// (union-merge resolves concurrent appends). Internal step of Send, public
	/// for the interop tests.</summary>
	public void PushWithRetry()
	{
		(int Code, string StdOut, string StdErr) result = default;
		for (int i = 0; i < 3; i++)
		{
			result = Git("push");
			if (result.Code == 0) return;
			PullRebase();
		}
		throw new InvalidOperationException($"push failed after retries: {result.StdErr}");
	}

	// ----- recv ------------------------------------------------------------ //

	public IReadOnlyList<Message> Recv(string? sinceId = null)
	{
		using var _ = AcquireSendLock();
		if (HasRemote()) PullRebase();
		return RecvResolved(sinceId);
	}

	private IReadOnlyList<Message> RecvResolved(string? sinceId)
	{
		// Fast path: a full-length cursor that lands in the active tail means
		// everything after it is also in the active tail.
		if (sinceId is { Length: >= 32 })
		{
			var active = ReadAll(includeArchive: false);
			var hits = active.Select((m, i) => (m, i)).Where(t => t.m.Id.StartsWith(sinceId)).ToList();
			if (hits.Count == 1) return active.Skip(hits[0].i + 1).ToList();
			// 0 → archived/stale; >1 → ambiguous: fall through to full history.
		}
		var all = ReadAll(includeArchive: true);
		if (sinceId is null) return all;
		var matches = all.Select((m, i) => (m, i)).Where(t => t.m.Id.StartsWith(sinceId)).ToList();
		if (matches.Count == 1) return all.Skip(matches[0].i + 1).ToList();
		Console.Error.WriteLine(matches.Count == 0
			? $"securedchat: warning — since-id '{sinceId}' not found (stale cursor?); returning nothing"
			: $"securedchat: warning — since-id '{sinceId}' is ambiguous; returning nothing");
		return [];
	}

	private List<Message> ReadAll(bool includeArchive)
	{
		var msgs = new List<Message>();
		if (includeArchive && Directory.Exists(ArchiveDir))
			foreach (var seg in Directory.GetFiles(ArchiveDir, "chat-*.jsonl").OrderBy(p => p, StringComparer.Ordinal))
				msgs.AddRange(ReadFile(seg));
		msgs.AddRange(ReadFile(ChatFile));
		// Dedup by id, keeping the first (oldest) copy — guards a line landing in
		// BOTH an archive segment and the active file after a union-merge race.
		var seen = new HashSet<string>();
		var deduped = new List<Message>(msgs.Count);
		foreach (var m in msgs)
			if (seen.Add(m.Id)) deduped.Add(m);
		return deduped;
	}

	private static IEnumerable<Message> ReadFile(string path)
	{
		if (!File.Exists(path)) yield break;
		foreach (var raw in File.ReadLines(path))
		{
			var line = raw.Trim();
			if (line.Length == 0) continue;
			Message m;
			try { m = Message.FromJsonl(line); }
			catch (System.Text.Json.JsonException)
			{
				Console.Error.WriteLine($"securedchat: skipping unparseable line: {line[..Math.Min(80, line.Length)]}");
				continue;
			}
			if (m.Id.Length == 0)
			{
				Console.Error.WriteLine($"securedchat: skipping message with no id: {line[..Math.Min(80, line.Length)]}");
				continue;
			}
			yield return m;
		}
	}

	// ----- same-host advisory send lock (compatible with python's) ---------- //

	private sealed class SendLock(string path) : IDisposable
	{
		public FileStream? Stream;
		public System.Threading.Timer? Beat;
		public void Dispose()
		{
			// Stop the heartbeat and WAIT for any in-flight callback before
			// deleting: a beat that lands after the delete would stamp a fresh
			// mtime on a lock file a DIFFERENT holder has since created, making
			// it look freshly beaten by a process that no longer holds it.
			if (Beat is not null)
			{
				var done = new ManualResetEvent(false);
				if (Beat.Dispose(done)) done.WaitOne(TimeSpan.FromSeconds(2));
				done.Dispose();
			}
			Stream?.Dispose();
			try { File.Delete(path); } catch (IOException) { } catch (UnauthorizedAccessException) { }
		}
	}

	// Mirrors cli/transport.py's LOCK_* constants; same env vars, same meaning.
	// StaleAfter is a budget of MISSED HEARTBEATS (5 at these defaults), not a
	// guess at how long a send takes — see AcquireSendLock.
	private static readonly double LockHeartbeat = EnvDouble("SECUREDCHAT_LOCK_HEARTBEAT", 2.0);
	private static readonly double LockStaleAfter = EnvDouble("SECUREDCHAT_LOCK_STALE_AFTER", 10.0);
	private static readonly double LockMaxWait = EnvDouble("SECUREDCHAT_LOCK_MAX_WAIT", 300.0);

	private static double EnvDouble(string name, double fallback)
	{
		var raw = Environment.GetEnvironmentVariable(name);
		return double.TryParse(raw, System.Globalization.NumberStyles.Float,
			System.Globalization.CultureInfo.InvariantCulture, out var v) ? v : fallback;
	}

	/// <summary>True/false/null, where null means "cannot tell" — same contract as
	/// python's _pid_alive. Never terminates the probed process.</summary>
	private static bool? PidAlive(int pid)
	{
		if (pid <= 0) return null;
		try { using var p = Process.GetProcessById(pid); return !p.HasExited; }
		catch (ArgumentException) { return false; }   // no such process
		catch (InvalidOperationException) { return false; }
		catch (Exception) { return null; }            // access denied etc: unknown
	}

	/// <summary>The holder record python writes into the lock file, or null when the
	/// file says nothing useful (empty = a holder mid-acquire, or an older C# peer).
	/// Absence of the record is not a record of absence: callers fall back to mtime.</summary>
	private static (string Host, int Pid, string Identity, string Op)? ReadLockHolder(string path)
	{
		try
		{
			var text = File.ReadAllText(path);
			if (string.IsNullOrWhiteSpace(text)) return null;
			using var doc = System.Text.Json.JsonDocument.Parse(text);
			var root = doc.RootElement;
			if (root.ValueKind != System.Text.Json.JsonValueKind.Object) return null;
			var host = root.TryGetProperty("host", out var h) ? h.GetString() ?? "" : "";
			var ident = root.TryGetProperty("identity", out var i) ? i.GetString() ?? "" : "";
			var op = root.TryGetProperty("op", out var o) ? o.GetString() ?? "" : "";
			var pid = root.TryGetProperty("pid", out var p) && p.TryGetInt32(out var pv) ? pv : 0;
			return (host, pid, ident, op);
		}
		catch (IOException) { return null; }
		catch (UnauthorizedAccessException) { return null; }
		catch (System.Text.Json.JsonException) { return null; }
	}

	/// <summary>Best-effort advisory lock serializing log-mutating ops on ONE
	/// machine — same .send.lock file the python CLI uses, so a C# send and a
	/// python recv on the same host serialize against each other.
	///
	/// Review 2026-09-04 (finding #2), fixed in both peers together: the previous
	/// version broke the lock on <c>age > timeout OR now > deadline</c>, both fixed
	/// at 10s from acquisition — but one git call inside the critical section is
	/// bounded at 120s and a send makes several plus push retries, so a HEALTHY
	/// holder doing a 45s push had its lock broken and a sibling ran git
	/// concurrently in the same clone. Elapsed time was being read as death.
	///
	/// Now the holder PROVES liveness by touching the lock file every
	/// LockHeartbeat seconds, so age measures SILENCE. A waiter breaks instantly
	/// on a lock naming a dead pid on this host, breaks on a stopped heartbeat
	/// (which also covers other hosts and content-free locks written by older
	/// peers), and NEVER breaks a beating holder — past LockMaxWait it throws
	/// instead, so a caller still cannot block forever but fails visibly rather
	/// than by running two gits in one clone.</summary>
	private IDisposable AcquireSendLock(double timeoutSeconds = 0.0)
	{
		Directory.CreateDirectory(RoomDir);
		// A holder cannot prove itself alive faster than it can beat, so a budget
		// below a few heartbeats would break every live holder — the original bug
		// in miniature. Callers asking for less get the floor.
		var staleAfter = Math.Max(timeoutSeconds <= 0 ? LockStaleAfter : timeoutSeconds,
			LockHeartbeat * 3);
		var hardDeadline = DateTime.UtcNow.AddSeconds(LockMaxWait);
		while (true)
		{
			try
			{
				var s = new FileStream(LockFile, FileMode.CreateNew, FileAccess.Write, FileShare.None);
				var record = System.Text.Json.JsonSerializer.Serialize(new
				{
					pid = Environment.ProcessId,
					host = Environment.MachineName,
					identity = Identity,
					op = "send-lock",
					ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
				});
				try
				{
					var bytes = System.Text.Encoding.UTF8.GetBytes(record);
					s.Write(bytes, 0, bytes.Length);
					s.Flush();
				}
				catch (IOException) { }  // content aids waiters; the mtime is what binds
				var lockFile = LockFile;
				var period = TimeSpan.FromSeconds(LockHeartbeat);
				var beat = new System.Threading.Timer(_ =>
				{
					try { File.SetLastWriteTimeUtc(lockFile, DateTime.UtcNow); }
					catch (IOException) { }
					catch (UnauthorizedAccessException) { }
				}, null, period, period);
				return new SendLock(lockFile) { Stream = s, Beat = beat };
			}
			catch (IOException)
			{
				double age;
				try { age = (DateTime.UtcNow - File.GetLastWriteTimeUtc(LockFile)).TotalSeconds; }
				catch (FileNotFoundException) { continue; }
				var holder = ReadLockHolder(LockFile);
				var dead = holder is not null
					&& string.Equals(holder.Value.Host, Environment.MachineName,
						StringComparison.OrdinalIgnoreCase)
					&& PidAlive(holder.Value.Pid) == false;
				if (dead || age > staleAfter)
				{
					try { File.Delete(LockFile); }
					catch (UnauthorizedAccessException) { Thread.Sleep(100); }
					catch (IOException) { Thread.Sleep(100); }
					continue;
				}
				if (DateTime.UtcNow > hardDeadline)
				{
					var who = holder is null
						? "unknown holder (no pid in lock file)"
						: $"pid {holder.Value.Pid} on {holder.Value.Host} " +
						  $"({holder.Value.Identity}, op={holder.Value.Op})";
					throw new TimeoutException(
						$"{LockFile} still held after {LockMaxWait:G}s by {who}; last " +
						$"heartbeat {age:F1}s ago (< {staleAfter:G}s, so the holder is " +
						$"alive and was NOT broken). Set SECUREDCHAT_LOCK_MAX_WAIT to wait longer.");
				}
				Thread.Sleep(100);
			}
		}
	}
}
