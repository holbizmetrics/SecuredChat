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
		public void Dispose()
		{
			Stream?.Dispose();
			try { File.Delete(path); } catch (IOException) { } catch (UnauthorizedAccessException) { }
		}
	}

	/// <summary>Best-effort advisory lock serializing log-mutating ops on ONE
	/// machine — same .send.lock file the python CLI uses, so a C# send and a
	/// python recv on the same host serialize against each other. Stale locks
	/// (older than timeout) are broken rather than blocking forever.</summary>
	private IDisposable AcquireSendLock(double timeoutSeconds = 10.0)
	{
		Directory.CreateDirectory(RoomDir);
		var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
		while (true)
		{
			try
			{
				var s = new FileStream(LockFile, FileMode.CreateNew, FileAccess.Write, FileShare.None);
				return new SendLock(LockFile) { Stream = s };
			}
			catch (IOException)
			{
				double age;
				try { age = (DateTime.UtcNow - File.GetLastWriteTimeUtc(LockFile)).TotalSeconds; }
				catch (FileNotFoundException) { continue; }
				if (age > timeoutSeconds || DateTime.UtcNow > deadline)
				{
					try { File.Delete(LockFile); }
					catch (UnauthorizedAccessException) { Thread.Sleep(100); }
					catch (IOException) { Thread.Sleep(100); }
					continue;
				}
				Thread.Sleep(100);
			}
		}
	}
}
