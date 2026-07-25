using System.Text;

namespace SecuredChat.Terminal;

/// <summary>
/// Command registry + tokenizer — the CommandHost pattern lifted from
/// WinDbgAotExt (same Argv semantics: whitespace-split, double quotes group,
/// backslash escapes " and \). Commands are registered handlers; anything not
/// registered does not exist. That IS the security model for bus-driven
/// execution: the bus can only invoke the allowlist, never arbitrary shell.
/// </summary>
public sealed class CommandHost
{
	public delegate string Handler(IReadOnlyList<string> argv, string raw);

	private readonly Dictionary<string, (Handler Run, string Help)> _map =
		new(StringComparer.OrdinalIgnoreCase);

	public void Register(string name, string help, Handler handler) =>
		_map[name] = (handler, help);

	public IEnumerable<(string Name, string Help)> Commands =>
		_map.OrderBy(kv => kv.Key, StringComparer.OrdinalIgnoreCase)
			.Select(kv => (kv.Key, kv.Value.Help));

	public bool Knows(string name) => _map.ContainsKey(name);

	/// <summary>Run one command line. Never throws: errors come back as text —
	/// a bus peer must always get a reply, not a dead air exception.</summary>
	public string Run(string commandLine)
	{
		var argv = Argv(commandLine);
		if (argv.Count == 0) return "error: empty command";
		string name = argv[0];
		string raw = commandLine.Length > name.Length
			? commandLine[(commandLine.IndexOf(name, StringComparison.OrdinalIgnoreCase) + name.Length)..].TrimStart()
			: "";
		if (!_map.TryGetValue(name, out var cmd))
			return $"error: unknown command '{name}' (try: help)";
		try { return cmd.Run(argv.Skip(1).ToList(), raw); }
		catch (Exception ex) { return $"error: {name}: {ex.GetType().Name}: {ex.Message}"; }
	}

	/// <summary>Tokenizer, semantics identical to WinDbgAotExt's CommandHost.Argv
	/// (proven there by ArgvTests): backslash escapes " and \; double quotes
	/// toggle grouping; whitespace splits outside quotes.</summary>
	public static List<string> Argv(string input)
	{
		var list = new List<string>();
		if (string.IsNullOrWhiteSpace(input)) return list;

		var sb = new StringBuilder();
		bool inQuotes = false;

		for (int i = 0; i < input.Length; i++)
		{
			char c = input[i];

			if (c == '\\' && i + 1 < input.Length && (input[i + 1] == '"' || input[i + 1] == '\\'))
			{ sb.Append(input[i + 1]); i++; continue; }

			if (c == '"') { inQuotes = !inQuotes; continue; }

			if (!inQuotes && char.IsWhiteSpace(c))
			{ if (sb.Length > 0) { list.Add(sb.ToString()); sb.Clear(); } continue; }

			sb.Append(c);
		}

		if (sb.Length > 0) list.Add(sb.ToString());
		return list;
	}
}
