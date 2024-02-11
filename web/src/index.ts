import "./index.d";
import { getResults } from "./getResults";
import { getQueue } from "./getQueue";
import { createUI } from "./createUI";

export {};

const body = document.body;

const results = getResults();
const queue = getQueue();
const [
  ui,
  inputValueCb,
  searchResultCb,
  setVotesCb,
  showResult,
  setNowPlayingTrack,
  setText,
] = createUI(queue, body);

body.prepend(ui);

inputValueCb((e) => {
  console.log(e);
  showResult(results);
});

searchResultCb((e) => console.log(e.currentTarget));

setVotesCb(
  (e) => console.log(e.currentTarget),
  (e) => console.log(e.currentTarget)
);
setTimeout(() => setNowPlayingTrack("Gotek - Antistress"), 3000);
setTimeout(() => setText("now its fucking 420 sats per track!"), 6000);
