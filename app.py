def inject_globals(context):
    # Handle ORM objects to fix DetachedInstanceError
    expunge_all()  # Assuming this is available in the current session
    context['orm_objects'] = [obj.to_dict() for obj in context['.orm_objects']]
    return context
