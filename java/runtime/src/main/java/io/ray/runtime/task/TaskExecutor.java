package io.ray.runtime.task;

import com.google.common.base.Preconditions;
import io.ray.api.exception.RayTaskException;
import io.ray.api.id.ActorId;
import io.ray.api.id.JobId;
import io.ray.api.id.TaskId;
import io.ray.api.id.UniqueId;
import io.ray.runtime.RayRuntimeInternal;
import io.ray.runtime.functionmanager.JavaFunctionDescriptor;
import io.ray.runtime.functionmanager.RayFunction;
import io.ray.runtime.generated.Common.TaskType;
import io.ray.runtime.object.NativeRayObject;
import io.ray.runtime.object.ObjectSerializer;
import java.lang.reflect.InvocationTargetException;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentHashMap;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * The task executor, which executes tasks assigned by raylet continuously.
 */
public abstract class TaskExecutor<T extends TaskExecutor.ActorContext> {

  private static final Logger LOGGER = LoggerFactory.getLogger(TaskExecutor.class);

  protected final RayRuntimeInternal runtime;

  private final ConcurrentHashMap<UniqueId, T> actorContextMap = new ConcurrentHashMap<>();

  private final ThreadLocal<RayFunction> localRayFunction = new ThreadLocal<>();

  static class ActorContext {

    /**
     * The current actor object, if this worker is an actor, otherwise null.
     */
    Object currentActor = null;

    /**
     * The exception that failed the actor creation task, if any.
     */
    Throwable actorCreationException = null;
  }

  TaskExecutor(RayRuntimeInternal runtime) {
    this.runtime = runtime;
  }

  protected abstract T createActorContext();

  T getActorContext() {
    return actorContextMap.get(runtime.getWorkerContext().getCurrentWorkerId());
  }

  void setActorContext(T actorContext) {
    if (actorContext == null) {
      // ConcurrentHashMap doesn't allow null values. So just return here.
      return;
    }
    this.actorContextMap.put(runtime.getWorkerContext().getCurrentWorkerId(), actorContext);
  }

  private RayFunction getRayFunction(List<String> rayFunctionInfo) {
    JobId jobId = runtime.getWorkerContext().getCurrentJobId();
    JavaFunctionDescriptor functionDescriptor = parseFunctionDescriptor(rayFunctionInfo);
    return runtime.getFunctionManager().getFunction(jobId, functionDescriptor);
  }

  /**
   * The return value indicates which parameters are ByteBuffer.
   */
  protected boolean[] checkByteBufferArguments(List<String> rayFunctionInfo) {
    localRayFunction.set(null);
    try {
      localRayFunction.set(getRayFunction(rayFunctionInfo));
    } catch (Throwable e) {
      // Ignore the exception.
      return null;
    }
    Class<?>[] types = localRayFunction.get().executable.getParameterTypes();
    boolean[] results = new boolean[types.length];
    for (int i = 0; i < types.length; i++) {
      results[i] = types[i] == ByteBuffer.class;
    }
    return results;
  }

  protected List<NativeRayObject> execute(List<String> rayFunctionInfo,
                                          List<Object> argsBytes) {
    runtime.setIsContextSet(true);
    TaskType taskType = runtime.getWorkerContext().getCurrentTaskType();
    TaskId taskId = runtime.getWorkerContext().getCurrentTaskId();
    LOGGER.debug("Executing task {}", taskId);

    T actorContext = null;
    if (taskType == TaskType.ACTOR_CREATION_TASK) {
      actorContext = createActorContext();
      setActorContext(actorContext);
    } else if (taskType == TaskType.ACTOR_TASK) {
      actorContext = getActorContext();
      Preconditions.checkNotNull(actorContext);
    }

    List<NativeRayObject> returnObjects = new ArrayList<>();
    ClassLoader oldLoader = Thread.currentThread().getContextClassLoader();
    RayFunction rayFunction = localRayFunction.get();
    try {
      // Find the executable object.
      if (rayFunction == null) {
        // Failed to get RayFunction in checkByteBufferArguments. Redo here to throw
        // the exception again.
        rayFunction = getRayFunction(rayFunctionInfo);
      }
      Thread.currentThread().setContextClassLoader(rayFunction.classLoader);
      runtime.getWorkerContext().setCurrentClassLoader(rayFunction.classLoader);

      // Get local actor object and arguments.
      Object actor = null;
      if (taskType == TaskType.ACTOR_TASK) {
        if (actorContext.actorCreationException != null) {
          throw actorContext.actorCreationException;
        }
        actor = actorContext.currentActor;
      }
      Object[] args = ArgumentsBuilder
          .unwrap(argsBytes, rayFunction.executable.getParameterTypes());
      // Execute the task.
      Object result;
      try {
        if (!rayFunction.isConstructor()) {
          result = rayFunction.getMethod().invoke(actor, args);
        } else {
          result = rayFunction.getConstructor().newInstance(args);
        }
      } catch (InvocationTargetException e) {
        if (e.getCause() != null) {
          throw e.getCause();
        } else {
          throw e;
        }
      }
      // Set result
      if (taskType != TaskType.ACTOR_CREATION_TASK) {
        if (taskType == TaskType.ACTOR_TASK) {
          // TODO (kfstorm): handle checkpoint in core worker.
          maybeSaveCheckpoint(actor, runtime.getWorkerContext().getCurrentActorId());
        }
        if (rayFunction.hasReturn()) {
          returnObjects.add(ObjectSerializer.serialize(result));
        }
      } else {
        // TODO (kfstorm): handle checkpoint in core worker.
        maybeLoadCheckpoint(result, runtime.getWorkerContext().getCurrentActorId());
        actorContext.currentActor = result;
      }
      LOGGER.debug("Finished executing task {}", taskId);
    } catch (Throwable e) {
      LOGGER.error("Error executing task " + taskId, e);
      if (taskType != TaskType.ACTOR_CREATION_TASK) {
        boolean hasReturn = rayFunction != null && rayFunction.hasReturn();
        boolean isCrossLanguage = parseFunctionDescriptor(rayFunctionInfo).signature.equals("");
        if (hasReturn || isCrossLanguage) {
          returnObjects.add(ObjectSerializer
              .serialize(new RayTaskException("Error executing task " + taskId, e)));
        }
      } else {
        actorContext.actorCreationException = e;
      }
    } finally {
      Thread.currentThread().setContextClassLoader(oldLoader);
      runtime.getWorkerContext().setCurrentClassLoader(null);
      runtime.setIsContextSet(false);
    }
    return returnObjects;
  }

  private JavaFunctionDescriptor parseFunctionDescriptor(List<String> rayFunctionInfo) {
    Preconditions.checkState(rayFunctionInfo != null && rayFunctionInfo.size() == 3);
    return new JavaFunctionDescriptor(rayFunctionInfo.get(0), rayFunctionInfo.get(1),
        rayFunctionInfo.get(2));
  }

  protected abstract void maybeSaveCheckpoint(Object actor, ActorId actorId);

  protected abstract void maybeLoadCheckpoint(Object actor, ActorId actorId);
}
