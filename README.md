<!-- SPDX-FileCopyrightText: 2026 Fengrímur -->
<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- See NOTICE for additional terms. -->

[FengPurgeBot](https://en.wikipedia.org/wiki/User:FengPurgeBot) handles Wikipedia requests for cache purges and refreshes of page links, either for one page or for category and template fan-outs of up to 1,500 targets. It doesn't use real null edits, so it doesn't edit pages.
It follows the community request to [take over ProcBot 5 and 6](https://en.wikipedia.org/wiki/Wikipedia:Bot_requests#Taking_over_ProcBot_5_and_6).

This bot uses Python 3.13 and MariaDB. The database tests need MariaDB. The worker automatically rechecks that a request is still active before each purge.

Some scratchpad & brainstorming files haven’t been pushed, but any unclear decision can be explained on request.

## License

FengPurgeBot is licensed under the [GNU Affero General Public License v3.0 only](LICENSE), with the additional terms in [NOTICE](NOTICE). Important questions can be left on [my talk page](https://en.wikipedia.org/wiki/User_talk:Fengr%C3%ADmur). (I do encourage you to read the code first, if that’s an option, as I tried my best to make it understandable.)
