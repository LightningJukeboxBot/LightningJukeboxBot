# Messages

This document specifies the message format and flows between several components.

Messages are formatted in JSON and have at least a *topic* (string) field. Depending on the type, other fields may be added to the message.

```
{
	"topic":"search",
	"query":"rage against the machine",
	"from":"some reference of the sender"
}
```

When a component sends a message, it may include a *from* field (string). This is an identifier that the component can use to identify itself, or where other components can react to. When a message is a response to another message, a *to* field is included with the value of the *from* field of the message it is responding to.

## Issues

  * When another component receives a message and cannot answer, or triggers an error, it should return a status message with some explanation. Maybe a statuscode should be always included in a message.
  * The *from* and *to* attributes may be overspecified. It could also be a *reference* to some id used to connect request and response to each other.