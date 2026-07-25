using SecuredChat.Bus;

namespace SecuredChat.Terminal;

/// <summary>
/// The terminal as a bus identity: polls the room, executes messages of
/// kind "cmd" addressed to it through the CommandHost allowlist, and replies
/// with a reply_to-linked "result". Honors the two protocol behaviors the
/// interop harness surfaced:
///  - cursor lives in ~/.config/securedchat/cursors/&lt;room&gt;__&lt;identity&gt;,
///    same file the python CLI uses, so the agent survives restarts and its
///    cursor is visible to the same tooling;
///  - a fresh identity anchors at HEAD instead of replaying the room's
///    backlog as commands (the cold-cursor boot guard — executing months of
///    old cmds on first boot would be much worse than boot noise).
/// Policy mirrors the SessionStart hook's: a bus message is operator-equivalent
/// input WITHIN standing permissions — and here the standing permissions are
/// structural: the registered command set, nothing else.
/// </summary>
public sealed class BusAgent(GitBusTransport bus, CommandHost host)
{
	private static string CursorDir =>
		Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
			".config", "securedchat", "cursors");

	private string CursorFile
	{
		get
		{
			static string Safe(string s) => string.Concat(s.Select(c =>
				char.IsAsciiLetterOrDigit(c) || c is '.' or '_' or '-' ? c : '_'));
			return Path.Combine(CursorDir, $"{Safe(bus.Room)}__{Safe(bus.Identity)}");
		}
	}

	private string? ReadCursor()
	{
		try { var v = File.ReadAllText(CursorFile).Trim(); return v.Length > 0 ? v : null; }
		catch (FileNotFoundException) { return null; }
		catch (DirectoryNotFoundException) { return null; }
	}

	private void WriteCursor(string id)
	{
		Directory.CreateDirectory(CursorDir);
		File.WriteAllText(CursorFile, id + "\n");
	}

	private static bool AddressedTo(string identity, string? to) =>
		to is not null && string.Equals(to, identity, StringComparison.Ordinal);

	private bool _bootSawEmptyRoom;

	/// <summary>One poll step: pull, execute pending cmds, reply, advance the
	/// cursor. Returns the number of commands executed. Public so tests (and a
	/// REPL "pump" command) can drive it without a background loop.</summary>
	public int Step()
	{
		string? cursor = ReadCursor();
		var pending = bus.Recv(cursor);
		if (pending.Count == 0)
		{
			// Fresh identity + empty room: there IS no backlog. Remember that,
			// so the first message to ever arrive is treated as new (executed),
			// not anchored over as history.
			if (cursor is null) _bootSawEmptyRoom = true;
			return 0;
		}

		if (cursor is null && !_bootSawEmptyRoom)
		{
			// Fresh identity, non-empty room: anchor at HEAD, never execute the
			// backlog (executing months of old cmds would be worse than noise).
			WriteCursor(pending[^1].Id);
			Console.Error.WriteLine(
				$"securedchat: fresh identity '{bus.Identity}' — cursor anchored at HEAD " +
				$"({pending[^1].Id[..8]}); {pending.Count} historical message(s) skipped");
			return 0;
		}

		int executed = 0;
		foreach (var m in pending)
		{
			if (m.Kind == "cmd" && AddressedTo(bus.Identity, m.To) && m.From != bus.Identity)
			{
				string output = host.Run(m.Body);
				bus.Send(new Message
				{
					Ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
					Id = Guid.NewGuid().ToString(),
					From = bus.Identity,
					To = m.From,
					Kind = "result",
					Body = output,
					ReplyTo = m.Id,
				});
				executed++;
			}
			WriteCursor(m.Id); // advance per message: a crash never re-executes
		}
		return executed;
	}

	/// <summary>Poll loop. Ctrl-C (or the CancellationToken) stops it.</summary>
	public void RunLoop(double pollSeconds, CancellationToken ct)
	{
		Console.Error.WriteLine(
			$"securedchat: agent '{bus.Identity}' listening on room '{bus.Room}' " +
			$"(poll {pollSeconds:0.#}s, {host.Commands.Count()} registered commands)");
		while (!ct.IsCancellationRequested)
		{
			int n = Step();
			if (n > 0) Console.Error.WriteLine($"securedchat: executed {n} command(s)");
			ct.WaitHandle.WaitOne(TimeSpan.FromSeconds(pollSeconds));
		}
	}
}
