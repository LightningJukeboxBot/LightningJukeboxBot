# API documentation of the Lightning Jukebox Bot
## request current status
```
GET https://<host>/jukebox/api/<chat id>/status
```
The response is JSON formatted and looks like the following sample
```
{
  "now":{
    "title":"Swarms - I Gave You Everything"
  },
  "queue":[],
  "price":210,
  "donation":70,
  "status":200
}
```
When there are tracks in the Queue, the response looks like the following
```

```
## Searching for tracks
```
POST https://<host>/jukebox/api/<chat id>/search
```
The contents of the body is JSON formatted like this:
```
{"query":"rage"}
```
The search response is JSON formatted like this:
```
{
  "result": {
    "tracks":[
      {"title":"Rage Against The Machine - Killing In The Name","track_id":"59WN2psjkt1tyaxjspN8fp"},
      {"title":"Rage Against The Machine - Know Your Enemy","track_id":"1XTGyfJeMiZXrZ1W3NolcB"},
      {"title":"Rage Against The Machine - Bullet In The Head","track_id":"1WWgMk8nD79p8VeKFGYrOw"},
      {"title":"Rage Against The Machine - Wake Up","track_id":"2QiqwOVUctPRVggO9G1Zs5"},
      {"title":"Rage Against The Machine - Renegades Of Funk","track_id":"5YBVDvTSSSiqv7KZDeUlXA"},
      {"title":"Rage Against The Machine - Take The Power Back","track_id":"25CbtOzU8Pn17SAaXFjIR3"}
    ]
  },
  "status":200
}
```
## Requesting a track
A track is requested by posting the track_id from the search result.
```
POST https://<host>/jukebox/api/<chat id>/request
```
The body of the request contains the track id.
```
{"track_id":"1WWgMk8nD79p8VeKFGYrOw"}
```
The response of the request is a JSON object that contains the Lightning invoice
```
{
  "status":200,
  "result":{
    "title":"'Rage Against The Machine - Killing In The Name'",
    "amount":21,
    "payment_hash":"058d...e9d7",
    "payment_request":"lnbc210n1pjlpsw9pp5q...dsq4m5dzjqqd9w0le"
  }
}
```
## Querying the status of a payment
The status of an invoice can be requested executing the following request:
```
GET https://<host>/jukebox/api/<chat_id>/payment/<payment_hash>
```
The response of the request is a JSON object that contains the payment status
```
{
  "status":402,
  "message":"Invoice not paid"
}
```
or when the invoice is paid
```
{
  "status":200,
  "message":"Invoice paid"
}
```



```
