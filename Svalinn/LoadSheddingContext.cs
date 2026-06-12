using Microsoft.AspNetCore.Http;

namespace Svalinn;

public sealed record LoadSheddingContext(
    HttpContext HttpContext,
    RequestPriority Priority,
    SystemState.SystemState State);
