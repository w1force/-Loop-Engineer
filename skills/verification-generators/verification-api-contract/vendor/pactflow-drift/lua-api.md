# Adapted deterministic Drift lifecycle patterns

This reference adapts PactFlow Drift Lua lifecycle guidance from the source and commit in
../../provenance.yaml. See LICENSE for the MIT terms.

Drift embeds Lua 5.4. Use lifecycle hooks only for deterministic fixture setup, independent
state inspection, and cleanup. Avoid current time, unseeded math.random, external network
calls, and hidden mutable module state.

## Events

| Event | Purpose |
| --- | --- |
| testcase:started | Create suite namespace and shared isolated prerequisites |
| operation:started | Create the exact pre-state for one operation |
| operation:prepared | Apply frozen request values after expression resolution |
| operation:finished | Observe post-state and attempt per-case cleanup |
| operation:failed | Capture diagnostics and attempt the same cleanup path |
| testcase:finished | Final cleanup and cleanup-postcondition verification |
| http:request | Modify a request; it must return the modified data |

Do not assume operation:finished alone covers failures or cancellation. Generate a cleanup
command outside Drift as the authoritative always-run cleanup, and make Lua cleanup idempotent
as defense in depth.

## Deterministic setup and cleanup

    local server_url = os.getenv("SERVER_URL")
    local object_id = os.getenv("FROZEN_OBJECT_ID")

    local function delete_fixture()
      local result = http({
        url = server_url .. "/products/" .. object_id,
        method = "DELETE"
      })
      if result.status ~= 204 and result.status ~= 404 then
        error("fixture cleanup failed: " .. tostring(result.status))
      end
    end

    return {
      event_handlers = {
        ["operation:started"] = function(event, data)
          delete_fixture()
          local result = http({
            url = server_url .. "/products",
            method = "POST",
            body = { id = object_id, name = "frozen-product", price = 9.99 }
          })
          if result.status ~= 201 then
            error("fixture setup failed: " .. tostring(result.status))
          end
        end,
        ["operation:finished"] = function(event, data)
          delete_fixture()
        end,
        ["operation:failed"] = function(event, data)
          delete_fixture()
        end
      }
    }

The manifest must also define a Coordinator-run cleanup argv and a postcondition such as a
trusted store lookup returning absent. Hook success without postcondition evidence is
insufficient.

## Independent side-effect observation

The built-in http function returns status, headers, and body. It may query a trusted fixture
adapter, but do not use the mutation endpoint itself as the sole proof of durable state. Record
the observer endpoint or store query as an oracle artifact and bind it by digest.

Any exported function used in YAML must return a value derived solely from frozen environment
inputs. Materialize the resolved value in the case input before Coordinator freeze.
