using System.Text.Encodings.Web;
using System.Text.Json;

namespace SecuredChat.Bus;

/// <summary>
/// One bus message. Wire-compatible with SecuredChat's cli/transport.py Message:
/// JSONL object with keys ts, id, from, to, kind, body (+ optional reply_to,
/// sig, sig_alg, sig_v). "to": null = broadcast within the room. Parsing is
/// tolerant by design: unknown keys are ignored, missing ones default —
/// only a JSON syntax error makes a line unparseable.
/// </summary>
public sealed record Message
{
	public double Ts { get; init; }
	public string Id { get; init; } = "";
	public string From { get; init; } = "";
	public string? To { get; init; }
	public string Kind { get; init; } = "msg";
	public string Body { get; init; } = "";
	public string? ReplyTo { get; init; }
	public string? Sig { get; init; }
	public string? SigAlg { get; init; }
	public int? SigV { get; init; }

	public static Message New(string from, string? to, string body,
		string kind = "msg", string? replyTo = null) => new()
	{
		Ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
		Id = Guid.NewGuid().ToString(),   // uuid4-format, matching python's uuid.uuid4()
		From = from,
		To = to,
		Kind = kind,
		Body = body,
		ReplyTo = replyTo,
	};

	// Matches python json.dumps(..., ensure_ascii=False): non-ASCII passes through
	// unescaped. UnsafeRelaxedJsonEscaping is safe here — output is a data file,
	// never embedded in HTML.
	private static readonly JsonWriterOptions WriterOptions = new()
	{
		Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
		Indented = false,
	};

	/// <summary>Serialize to one JSONL line (no trailing newline). Key order and
	/// presence rules mirror transport.py to_jsonl exactly: reply_to only when
	/// set; sig implies sig_alg (default "ssh"); sig_v only when truthy.</summary>
	public string ToJsonl()
	{
		using var stream = new MemoryStream();
		using (var w = new Utf8JsonWriter(stream, WriterOptions))
		{
			w.WriteStartObject();
			w.WriteNumber("ts", Ts);
			w.WriteString("id", Id);
			w.WriteString("from", From);
			if (To is null) w.WriteNull("to"); else w.WriteString("to", To);
			w.WriteString("kind", Kind);
			w.WriteString("body", Body);
			if (ReplyTo is not null) w.WriteString("reply_to", ReplyTo);
			if (Sig is not null)
			{
				w.WriteString("sig", Sig);
				w.WriteString("sig_alg", SigAlg ?? "ssh");
				if (SigV is > 0) w.WriteNumber("sig_v", SigV.Value);
			}
			w.WriteEndObject();
		}
		return System.Text.Encoding.UTF8.GetString(stream.ToArray());
	}

	/// <summary>Parse one JSONL line. Tolerant: ignores unknown keys, defaults
	/// missing ones (accepts both "from" and legacy "from_"). Throws JsonException
	/// only on a JSON syntax error — the caller (recv) skips those lines.</summary>
	public static Message FromJsonl(string line)
	{
		using var doc = JsonDocument.Parse(line);
		var root = doc.RootElement;

		static string? Str(JsonElement e, string name) =>
			e.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;

		double ts = 0.0;
		if (root.TryGetProperty("ts", out var tsProp))
			ts = tsProp.ValueKind switch
			{
				JsonValueKind.Number => tsProp.GetDouble(),
				JsonValueKind.String when double.TryParse(tsProp.GetString(),
					System.Globalization.NumberStyles.Float,
					System.Globalization.CultureInfo.InvariantCulture, out var v) => v,
				_ => 0.0,
			};

		int? sigV = null;
		if (root.TryGetProperty("sig_v", out var svProp) && svProp.ValueKind == JsonValueKind.Number
			&& svProp.TryGetInt32(out var sv))
			sigV = sv;

		return new Message
		{
			Ts = ts,
			Id = Str(root, "id") ?? "",
			From = Str(root, "from") ?? Str(root, "from_") ?? "",
			To = Str(root, "to"),
			Kind = Str(root, "kind") ?? "msg",
			Body = Str(root, "body") ?? "",
			ReplyTo = Str(root, "reply_to"),
			Sig = Str(root, "sig"),
			SigAlg = Str(root, "sig_alg"),
			SigV = sigV,
		};
	}
}
